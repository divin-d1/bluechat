"""Host-coordinated group file offers and bounded per-recipient relay."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from bluechat.chat.session import AsyncChatSession
from bluechat.chat.group import GroupRouter
from bluechat.errors import TransferError
from bluechat.protocol.messages import Message
from bluechat.transfer.files import safe_filename

logger = logging.getLogger(__name__)
_TRANSFER_QUEUE_SIZE = 8


@dataclass(slots=True)
class _Transfer:
    transfer_id: str
    sender_id: str
    sender: AsyncChatSession
    recipients: dict[str, AsyncChatSession]
    queues: dict[str, asyncio.Queue[Message]] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None


class GroupFileRelay:
    """Broker an offer and relay bytes only to recipients who approved it.

    Control-message delivery runs independently of room text routing. The
    transfer queue is bounded so a slow peer can fail its transfer without
    blocking ordinary room messages or exhausting memory.
    """

    def __init__(self, router: GroupRouter, *, peer_timeout: float = 20.0) -> None:
        if peer_timeout <= 0:
            raise ValueError("peer_timeout must be positive")
        self.router = router
        self.peer_timeout = peer_timeout
        self._transfers: dict[str, _Transfer] = {}
        self._closed = False

    async def handle(self, participant_id: str, message: Message) -> None:
        """Consume a participant control packet from ``GroupRouter``."""
        transfer_id = message.payload.get("transfer_id")
        if not isinstance(transfer_id, str) or not 1 <= len(transfer_id) <= 128:
            return
        current = self._transfers.get(transfer_id)
        if message.type == "FILE_OFFER":
            if current is not None or self._closed:
                return
            sessions = self.router.peer_sessions
            peers = {pid: session for pid, session in sessions.items() if pid != participant_id}
            if not peers:
                return
            transfer = _Transfer(
                transfer_id,
                participant_id,
                sessions[participant_id],
                peers,
                {pid: asyncio.Queue(_TRANSFER_QUEUE_SIZE) for pid in peers},
            )
            self._transfers[transfer_id] = transfer
            transfer.task = asyncio.create_task(
                self._run(transfer, message), name=f"bluechat-group-file-{transfer_id[:8]}"
            )
            return
        if current is None:
            return
        # Sender stream messages go to the coordinator. Recipient decisions,
        # chunk acknowledgements and completion acknowledgements go to their
        # own bounded queue.
        destination = current.queues.get(participant_id)
        if participant_id == current.sender_id:
            destination = current.queues.setdefault(
                participant_id, asyncio.Queue(_TRANSFER_QUEUE_SIZE)
            )
        if destination is not None:
            try:
                destination.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("Dropping excess group transfer control data")

    async def _next(self, transfer: _Transfer, peer_id: str, timeout: float) -> Message:
        return await asyncio.wait_for(transfer.queues[peer_id].get(), timeout=timeout)

    async def _run(self, transfer: _Transfer, offer: Message) -> None:
        accepted: dict[str, AsyncChatSession] = {}
        try:
            # Validate essential metadata before exposing the offer.
            filename = offer.payload.get("filename")
            size = offer.payload.get("size")
            digest = offer.payload.get("sha256")
            if (
                not isinstance(filename, str)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or size > 2 * 1024 * 1024 * 1024
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)
            ):
                raise TransferError("The group file offer is malformed")
            mime = offer.payload.get("mime_type")
            if (
                not isinstance(mime, str)
                or len(mime) > 128
                or not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", mime)
            ):
                raise TransferError("The group file type metadata is malformed")
            safe_name = safe_filename(filename)
            safe_offer = dict(offer.payload)
            safe_offer["filename"] = safe_name

            await asyncio.gather(
                *(
                    session.send_control("FILE_OFFER", safe_offer)
                    for session in transfer.recipients.values()
                ),
                return_exceptions=True,
            )
            pending = set(transfer.recipients)
            deadline = asyncio.get_running_loop().time() + 120
            while pending:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                reads = {
                    asyncio.create_task(self._next(transfer, peer_id, remaining)): peer_id
                    for peer_id in pending
                }
                done, unfinished = await asyncio.wait(reads, return_when=asyncio.FIRST_COMPLETED)
                for task in unfinished:
                    task.cancel()
                await asyncio.gather(*unfinished, return_exceptions=True)
                for task in done:
                    peer_id = reads[task]
                    pending.discard(peer_id)
                    try:
                        response = task.result()
                    except (asyncio.TimeoutError, ConnectionError):
                        continue
                    if response.type == "FILE_ACCEPT":
                        accepted[peer_id] = transfer.recipients[peer_id]
            if not accepted:
                await transfer.sender.send_control(
                    "FILE_REJECT", {"transfer_id": transfer.transfer_id}
                )
                return
            await transfer.sender.send_control("FILE_ACCEPT", {"transfer_id": transfer.transfer_id})

            sender_queue = transfer.queues.setdefault(
                transfer.sender_id, asyncio.Queue(_TRANSFER_QUEUE_SIZE)
            )
            active = dict(accepted)
            expected_index = 0
            while active:
                item = await asyncio.wait_for(sender_queue.get(), timeout=120)
                if item.type == "FILE_START":
                    await self._broadcast(active, item)
                elif item.type == "FILE_CHUNK":
                    index = item.payload.get("index")
                    if type(index) is not int or index != expected_index:
                        raise TransferError("The sender sent an out-of-order group file chunk")

                    async def relay_to(
                        peer_id: str,
                        session: AsyncChatSession,
                        payload: dict[str, object],
                        chunk_index: int,
                    ) -> bool:
                        try:
                            await asyncio.wait_for(
                                session.send_control("FILE_CHUNK", payload),
                                timeout=self.peer_timeout,
                            )
                            acknowledgement = await self._next(transfer, peer_id, self.peer_timeout)
                            return (
                                acknowledgement.type == "FILE_CHUNK"
                                and acknowledgement.payload.get("ack") == chunk_index
                            )
                        except (asyncio.TimeoutError, ConnectionError, OSError):
                            return False

                    peers = tuple(active.items())
                    results = await asyncio.gather(
                        *(
                            relay_to(peer_id, session, item.payload, index)
                            for peer_id, session in peers
                        )
                    )
                    failed = {
                        peer_id
                        for (peer_id, _session), succeeded in zip(peers, results, strict=True)
                        if not succeeded
                    }
                    for peer_id in failed:
                        active.pop(peer_id, None)
                    if failed:
                        await asyncio.gather(
                            *(
                                transfer.recipients[peer_id].send_control(
                                    "FILE_FAILED",
                                    {
                                        "transfer_id": transfer.transfer_id,
                                        "reason": "The recipient could not keep up with this transfer",
                                    },
                                )
                                for peer_id in failed
                            ),
                            return_exceptions=True,
                        )
                    await transfer.sender.send_control(
                        "FILE_CHUNK", {"transfer_id": transfer.transfer_id, "ack": index}
                    )
                    expected_index += 1
                    if not active:
                        await transfer.sender.send_control(
                            "FILE_FAILED",
                            {
                                "transfer_id": transfer.transfer_id,
                                "reason": "No recipients remain connected",
                            },
                        )
                        return
                elif item.type == "FILE_COMPLETE":
                    await self._broadcast(active, item)
                    completion_results = await asyncio.gather(
                        *(self._await_complete(transfer, peer_id) for peer_id in active),
                        return_exceptions=True,
                    )
                    completed = sum(result is True for result in completion_results)
                    await transfer.sender.send_control(
                        "FILE_COMPLETE",
                        {"transfer_id": transfer.transfer_id, "recipients": completed},
                    )
                    return
                elif item.type == "FILE_FAILED":
                    await self._broadcast(active, item)
                    return
                else:
                    raise TransferError("Unexpected group transfer control message")
        except (asyncio.TimeoutError, ConnectionError, OSError, TransferError) as exc:
            try:
                await transfer.sender.send_control(
                    "FILE_FAILED",
                    {"transfer_id": transfer.transfer_id, "reason": str(exc)[:120]},
                )
            except Exception:
                pass
        finally:
            self._transfers.pop(transfer.transfer_id, None)

    async def _await_complete(self, transfer: _Transfer, peer_id: str) -> bool:
        try:
            message = await self._next(transfer, peer_id, self.peer_timeout)
            return message.type == "FILE_COMPLETE"
        except (asyncio.TimeoutError, ConnectionError):
            return False

    async def _broadcast(self, peers: dict[str, AsyncChatSession], message: Message) -> None:
        outcomes = await asyncio.gather(
            *(peer.send_control(message.type, message.payload) for peer in peers.values()),
            return_exceptions=True,
        )
        for peer_id, result in zip(tuple(peers), outcomes, strict=True):
            if isinstance(result, BaseException):
                peers.pop(peer_id, None)

    async def close(self) -> None:
        self._closed = True
        tasks = [transfer.task for transfer in self._transfers.values() if transfer.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._transfers.clear()
