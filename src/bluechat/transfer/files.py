"""Streaming file-transfer primitives and safe destination handling."""

from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from bluechat.errors import TransferError

CHUNK_SIZE = 16 * 1024
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_INVALID = re.compile(r'[<>:"|?*]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class FileOffer:
    transfer_id: str
    filename: str
    size: int
    mime_type: str
    sha256: str


def safe_filename(value: str) -> str:
    """Reduce an untrusted peer filename to one safe basename."""
    if not isinstance(value, str) or not value or len(value) > 255:
        raise TransferError("The received filename is invalid")
    if "\x00" in value:
        raise TransferError("The received filename contains invalid characters")
    # Treat both path separator styles as separators independent of host OS.
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    name = _WINDOWS_INVALID.sub("_", _CONTROL_CHARS.sub("_", name)).strip().strip(" .")
    if name in {"", ".", ".."}:
        raise TransferError("The received filename is invalid")
    stem = name.split(".", 1)[0].rstrip(" .").upper()
    if stem in _WINDOWS_RESERVED:
        name = f"_{name}"
    if len(name.encode("utf-8")) > 240:
        suffix = Path(name).suffix[:32]
        stem = Path(name).stem.encode("utf-8")[:200].decode("utf-8", "ignore")
        name = stem + suffix
    return name


def file_category(filename: str, mime_type: str | None = None) -> str:
    mime = (mime_type or mimetypes.guess_type(filename)[0] or "").lower()
    suffix = Path(filename).suffix.lower()
    if mime.startswith("image/") or suffix in {
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".webp",
        ".bmp",
        ".tiff",
    }:
        return "Images"
    if mime.startswith("video/") or suffix in {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mpeg"}:
        return "Videos"
    return "Files"


def unique_destination(directory: Path, filename: str) -> Path:
    """Choose a non-existing path without following a peer-supplied path."""
    directory = directory.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    safe = safe_filename(filename)
    candidate = directory / safe
    index = 1
    while candidate.exists():
        stem, suffix = Path(safe).stem, Path(safe).suffix
        candidate = directory / f"{stem} ({index}){suffix}"
        index += 1
    if candidate.parent != directory:
        raise TransferError("The destination path escaped the download directory")
    return candidate


def stream_file(path: Path, *, chunk_size: int = CHUNK_SIZE) -> tuple[int, str, Iterator[bytes]]:
    """Open a regular file and return size, digest and a replayable chunk iterator.

    The digest is computed in a first bounded-memory pass; sending is a second
    pass, so the sender can include an integrity digest in FILE_OFFER.
    """
    if chunk_size < 1 or chunk_size > 256 * 1024:
        raise ValueError("chunk_size must be between 1 and 256 KiB")
    source = path.expanduser()
    try:
        stat = source.stat()
        if not source.is_file() or stat.st_size > MAX_FILE_SIZE:
            raise TransferError("Only regular files up to 2 GiB can be shared")
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for part in iter(lambda: stream.read(chunk_size), b""):
                digest.update(part)
    except OSError as exc:
        raise TransferError(f"Could not read the selected file: {exc}") from exc

    def chunks() -> Iterator[bytes]:
        try:
            with source.open("rb") as stream:
                for part in iter(lambda: stream.read(chunk_size), b""):
                    yield part
        except OSError as exc:
            raise TransferError(f"File read failed during transfer: {exc}") from exc

    return stat.st_size, digest.hexdigest(), chunks()


def receive_stream(
    chunks: Iterator[bytes],
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> Path:
    """Write to an exclusive temporary file, verify, then atomically publish."""
    if expected_size < 0 or expected_size > MAX_FILE_SIZE:
        raise TransferError("The offered file size is outside the supported limit")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise TransferError("The offered file checksum is malformed")
    directory = destination.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = unique_destination(directory, destination.name)
    temporary = target.with_name(f".{target.name}.{os.urandom(8).hex()}.part")
    digest = hashlib.sha256()
    count = 0
    try:
        with temporary.open("xb") as output:
            for chunk in chunks:
                if not isinstance(chunk, bytes) or not chunk:
                    raise TransferError("The transfer contained an invalid chunk")
                count += len(chunk)
                if count > expected_size:
                    raise TransferError("The transfer exceeded its declared size")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if count != expected_size or not hmac.compare_digest(
            digest.hexdigest(), expected_sha256.lower()
        ):
            raise TransferError("File integrity verification failed")
        # Recheck the target immediately before publication; never overwrite.
        final_path = unique_destination(directory, target.name)
        # A hard link publishes atomically and fails instead of replacing a
        # file created in the small race after unique_destination().
        os.link(temporary, final_path)
        temporary.unlink()
        return final_path
    except (OSError, TransferError) as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, TransferError):
            raise
        raise TransferError(f"Could not save the received file: {exc}") from exc
