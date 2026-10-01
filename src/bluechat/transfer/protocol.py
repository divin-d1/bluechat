"""Bounded, acknowledged file streaming over an encrypted chat session."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import mimetypes
import os
import re
import secrets
from pathlib import Path
from typing import Awaitable, Callable

from bluechat.errors import TransferError
from bluechat.transfer.files import CHUNK_SIZE, MAX_FILE_SIZE, safe_filename, unique_destination

ProgressCallback = Callable[[int, int], None]
ApprovalCallback = Callable[[str, int, str], Awaitable[Path | None]]
logger = logging.getLogger(__name__)


class FileTransferManager:
    """Transfer files without buffering file contents into memory.

    Acknowledging each bounded chunk supplies backpressure. Control and text
    messages remain on the session's ordinary channels and retain priority.
    """

    def __init__(self, *, chunk_size: int = 8 * 1024) -> None:
        if not 512 <= chunk_size <= 12 * 1024:
            raise ValueError("chunk_size must be between 512 bytes and 12 KiB")
        self.chunk_size = chunk_size

    async def send_file(
        self, session, path: Path, *, progress: ProgressCallback | None = None
    ) -> str:
        source = path.expanduser()
        try:
            stat = source.stat()
            if not source.is_file() or stat.st_size > MAX_FILE_SIZE:
                raise TransferError("Only regular files up to 2 GiB can be sent")
            digest = await asyncio.to_thread(_hash_file, source)
        except OSError as exc:
            raise TransferError(f"Could not read the selected file: {exc}") from exc
        transfer_id = secrets.token_urlsafe(16)
        session.open_transfer(transfer_id)
        mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        try:
            await session.send_control(
                "FILE_OFFER",
                {
                    "transfer_id": transfer_id,
                    "filename": source.name,
                    "size": stat.st_size,
                    "mime_type": mime,
                    "sha256": digest,
                },
            )
            response = await session.receive_transfer_control(transfer_id, timeout=120)
            if response.type == "FILE_REJECT":
                raise TransferError("The recipient declined the file")
            if response.type != "FILE_ACCEPT":
                raise TransferError("The recipient returned an invalid file-transfer response")
            await session.send_control("FILE_START", {"transfer_id": transfer_id})
            sent = 0
            index = 0
            with source.open("rb") as stream:
                while chunk := await asyncio.to_thread(stream.read, self.chunk_size):
                    if sent + len(chunk) > stat.st_size or sent + len(chunk) > MAX_FILE_SIZE:
                        raise TransferError("The selected file changed size during transfer")
                    packet = base64.b64encode(chunk).decode("ascii")
                    await session.send_control(
                        "FILE_CHUNK",
                        {
                            "transfer_id": transfer_id,
                            "index": index,
                            "data": packet,
                        },
                    )
                    ack = await session.receive_transfer_control(transfer_id, timeout=60)
                    if (
                        ack.type != "FILE_CHUNK"
                        or ack.payload.get("transfer_id") != transfer_id
                        or type(ack.payload.get("ack")) is not int
                        or ack.payload.get("ack") != index
                    ):
                        raise TransferError("The recipient did not acknowledge a file chunk")
                    sent += len(chunk)
                    index += 1
                    if progress:
                        _report_progress(progress, sent, stat.st_size)
            if sent != stat.st_size:
                raise TransferError("The selected file changed size during transfer")
            await session.send_control(
                "FILE_COMPLETE",
                {
                    "transfer_id": transfer_id,
                    "size": sent,
                    "sha256": digest,
                },
            )
            done = await session.receive_transfer_control(transfer_id, timeout=60)
            if done.type != "FILE_COMPLETE" or done.payload.get("transfer_id") != transfer_id:
                raise TransferError("The recipient could not verify the completed file")
        except (OSError, asyncio.TimeoutError) as exc:
            raise TransferError(f"File transfer was interrupted: {exc}") from exc
        except TransferError as exc:
            try:
                await session.send_control(
                    "FILE_FAILED",
                    {
                        "transfer_id": transfer_id,
                        "reason": str(exc)[:160],
                    },
                )
            except Exception as send_error:
                logger.debug(
                    "Could not notify the peer of transfer failure (%s)", type(send_error).__name__
                )
            raise
        finally:
            session.close_transfer(transfer_id)
        return transfer_id

    async def receive_file(
        self,
        session,
        offer,
        approve: ApprovalCallback,
        *,
        progress: ProgressCallback | None = None,
    ) -> Path | None:
        payload = offer.payload
        transfer_id = payload.get("transfer_id")
        filename = payload.get("filename")
        size = payload.get("size")
        digest = payload.get("sha256")
        mime = payload.get("mime_type")
        if (
            not isinstance(transfer_id, str)
            or len(transfer_id) > 128
            or not isinstance(filename, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > MAX_FILE_SIZE
            or not isinstance(digest, str)
            or not isinstance(mime, str)
            or len(mime) > 128
            or not re.fullmatch(r"[0-9a-fA-F]{64}", digest or "")
        ):
            raise TransferError("The incoming file offer is malformed")
        if not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", mime):
            raise TransferError("The incoming file type metadata is invalid")
        safe = safe_filename(filename)
        destination = await approve(safe, size, mime)
        if destination is None:
            await session.send_control("FILE_REJECT", {"transfer_id": transfer_id})
            return None
        if destination.name != safe:
            destination = destination / safe
        parent = destination.parent.expanduser().resolve()
        parent.mkdir(parents=True, exist_ok=True)
        target = unique_destination(parent, safe)
        temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.part")
        session.open_transfer(transfer_id)
        try:
            await session.send_control("FILE_ACCEPT", {"transfer_id": transfer_id})
            start = await session.receive_transfer_control(transfer_id, timeout=60)
        except Exception:
            session.close_transfer(transfer_id)
            raise
        if start.type != "FILE_START" or start.payload.get("transfer_id") != transfer_id:
            session.close_transfer(transfer_id)
            raise TransferError("The sender did not start the approved transfer")
        count = 0
        index = 0
        hasher = hashlib.sha256()
        try:
            with temporary.open("xb") as output:
                while count < size:
                    packet = await session.receive_transfer_control(transfer_id, timeout=120)
                    if packet.type == "FILE_FAILED":
                        raise TransferError("The sender could not complete the file")
                    data_index, encoded = packet.payload.get("index"), packet.payload.get("data")
                    if (
                        packet.type != "FILE_CHUNK"
                        or packet.payload.get("transfer_id") != transfer_id
                        or type(data_index) is not int
                        or data_index != index
                        or not isinstance(encoded, str)
                        or len(encoded) > 4 * self.chunk_size
                    ):
                        raise TransferError("The sender sent an invalid file chunk")
                    try:
                        chunk = base64.b64decode(encoded, validate=True)
                    except (ValueError, TypeError) as exc:
                        raise TransferError("The sender sent malformed file data") from exc
                    if not chunk or len(chunk) > self.chunk_size or count + len(chunk) > size:
                        raise TransferError("The file chunk exceeds the declared size")
                    output.write(chunk)
                    hasher.update(chunk)
                    count += len(chunk)
                    await session.send_control(
                        "FILE_CHUNK", {"transfer_id": transfer_id, "ack": index}
                    )
                    index += 1
                    if progress:
                        _report_progress(progress, count, size)
                complete = await session.receive_transfer_control(transfer_id, timeout=60)
                actual = hasher.hexdigest()
                if (
                    complete.type != "FILE_COMPLETE"
                    or complete.payload.get("transfer_id") != transfer_id
                    or complete.payload.get("size") != size
                    or not hmac.compare_digest(actual, digest.lower())
                ):
                    raise TransferError("File integrity verification failed")
                output.flush()
                os.fsync(output.fileno())
            # Exclusive atomic publication avoids overwriting a concurrent file.
            target = unique_destination(parent, safe)
            os.link(temporary, target)
            temporary.unlink()
            await session.send_control(
                "FILE_COMPLETE", {"transfer_id": transfer_id, "sha256": actual}
            )
            return target
        except (OSError, asyncio.TimeoutError, TransferError) as exc:
            temporary.unlink(missing_ok=True)
            try:
                await session.send_control(
                    "FILE_FAILED",
                    {
                        "transfer_id": transfer_id,
                        "reason": str(exc)[:160],
                    },
                )
            except Exception as send_error:
                logger.debug(
                    "Could not notify the peer of transfer failure (%s)", type(send_error).__name__
                )
            if isinstance(exc, TransferError):
                raise
            raise TransferError(f"Could not complete file transfer: {exc}") from exc
        finally:
            session.close_transfer(transfer_id)
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                logger.warning(
                    "Could not remove an incomplete transfer file (%s)",
                    type(cleanup_error).__name__,
                )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_progress(callback: ProgressCallback, completed: int, total: int) -> None:
    try:
        callback(completed, total)
    except Exception as exc:
        logger.debug("A file-transfer progress callback failed (%s)", type(exc).__name__)
