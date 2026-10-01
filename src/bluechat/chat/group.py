"""Host-side group routing with bounded fan-out and duplicate suppression."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from collections.abc import Awaitable, Callable

from bluechat.protocol.messages import Message

from bluechat.chat.session import AsyncChatSession, ChatMessage

logger = logging.getLogger(__name__)
MAX_RECENT_MESSAGE_IDS = 4096


@dataclass(slots=True)
class _Peer:
    participant_id: str
    name: str
    session: AsyncChatSession
    task: asyncio.Task[None]
    writer: asyncio.Task[None]
    control_reader: asyncio.Task[None]


class GroupRouter:
    """Route messages through the host without blocking on slow peers.

    Each client has an independent bounded outbound queue. A peer that cannot
    keep up is disconnected from routing rather than stalling the room.
    """

    def __init__(
        self,
        room_id: str,
        *,
        queue_size: int = 64,
        on_peer_left: Callable[[str, str], Awaitable[None]] | None = None,
        on_control: Callable[[str, Message], Awaitable[None]] | None = None,
    ) -> None:
        if not room_id or queue_size < 1:
            raise ValueError("room_id and a positive queue size are required")
        self.room_id = room_id
        self.queue_size = queue_size
        self._peers: dict[str, _Peer] = {}
        self._outbound: dict[str, asyncio.Queue[ChatMessage | None]] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._lock = asyncio.Lock()
        self._closed = False
        self.on_peer_left = on_peer_left
        self.on_control = on_control
        self.incoming: asyncio.Queue[ChatMessage] = asyncio.Queue(maxsize=queue_size * 4)

    @property
    def participants(self) -> tuple[tuple[str, str], ...]:
        return tuple((peer.participant_id, peer.name) for peer in self._peers.values())

    @property
    def peer_sessions(self) -> dict[str, AsyncChatSession]:
        """Return a snapshot of active participant sessions for room services."""
        return {participant_id: peer.session for participant_id, peer in self._peers.items()}

    async def add(self, participant_id: str, name: str, session: AsyncChatSession) -> None:
        if not participant_id or participant_id in self._peers:
            raise ValueError("participant ID is empty or already connected")
        outbound: asyncio.Queue[ChatMessage | None] = asyncio.Queue(self.queue_size)
        async with self._lock:
            if self._closed:
                raise RuntimeError("group router is closed")
            self._outbound[participant_id] = outbound
            writer = asyncio.create_task(self._writer(participant_id, session, outbound))
            control_reader = asyncio.create_task(
                self._control_reader(participant_id, session),
                name=f"bluechat-control-{participant_id}",
            )
            peer = _Peer(participant_id, name, session, writer, writer, control_reader)
            reader = asyncio.create_task(
                self._reader(peer), name=f"bluechat-route-{participant_id}"
            )
            peer.task = reader
            self._peers[participant_id] = peer

    async def remove(self, participant_id: str) -> bool:
        async with self._lock:
            peer = self._peers.pop(participant_id, None)
            outbound = self._outbound.pop(participant_id, None)
        if peer is None:
            return False
        if outbound is not None:
            try:
                outbound.put_nowait(None)
            except asyncio.QueueFull:
                pass
        for task in (peer.task, peer.writer, peer.control_reader):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        if self.on_peer_left is not None:
            await self.on_peer_left(participant_id, peer.name)
        return True

    async def broadcast_event(self, kind: str, payload: dict[str, object]) -> None:
        """Send a control event to all peers without awaiting individual writes."""
        for peer_id, peer in tuple(self._peers.items()):
            try:
                await peer.session.send_control(kind, payload)
            except Exception as exc:
                logger.debug("Could not send group event to peer (%s)", type(exc).__name__)
                await self.remove(peer_id)

    async def publish(self, message: ChatMessage, *, exclude: str | None = None) -> None:
        """Queue a host-authored message or validated routed message for peers."""
        if message.room_id != self.room_id or not message.message_id:
            raise ValueError("message belongs to another room or has no message ID")
        if message.message_id in self._seen:
            return
        self._seen[message.message_id] = None
        if len(self._seen) > MAX_RECENT_MESSAGE_IDS:
            self._seen.popitem(last=False)
        for peer_id, queue in tuple(self._outbound.items()):
            if peer_id == exclude:
                continue
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                await self.remove(peer_id)

    async def _reader(self, source: _Peer) -> None:
        try:
            while not self._closed:
                message = await source.session.receive()
                if message.room_id != self.room_id or message.sender_id != source.participant_id:
                    continue
                if not message.message_id or message.message_id in self._seen:
                    continue
                try:
                    self.incoming.put_nowait(message)
                except asyncio.QueueFull:
                    logger.warning("Host group event queue is full; dropping an incoming message")
                self._seen[message.message_id] = None
                if len(self._seen) > MAX_RECENT_MESSAGE_IDS:
                    self._seen.popitem(last=False)
                for peer_id, queue in tuple(self._outbound.items()):
                    if peer_id == source.participant_id:
                        continue
                    try:
                        queue.put_nowait(message)
                    except asyncio.QueueFull:
                        logger.warning("Removing a slow participant from group routing")
                        await self.remove(peer_id)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.warning("Group peer reader stopped (%s)", type(exc).__name__)
        finally:
            if not self._closed:
                await self.remove(source.participant_id)

    async def _writer(
        self,
        participant_id: str,
        session: AsyncChatSession,
        outbound: asyncio.Queue[ChatMessage | None],
    ) -> None:
        try:
            while True:
                message = await outbound.get()
                if message is None:
                    return
                await session.send_chat_message(message)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.warning("Group peer writer stopped (%s)", type(exc).__name__)
            await self.remove(participant_id)

    async def _control_reader(self, participant_id: str, session: AsyncChatSession) -> None:
        """Give host services a dedicated, bounded control-message consumer."""
        try:
            while not self._closed:
                message = await session.receive_control()
                if self.on_control is not None:
                    await self.on_control(participant_id, message)
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:
            logger.warning("Group control reader stopped (%s)", type(exc).__name__)
        finally:
            if not self._closed:
                await self.remove(participant_id)

    async def close(self) -> None:
        self._closed = True
        peers = tuple(self._peers.values())
        self._peers.clear()
        for queue in self._outbound.values():
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._outbound.clear()
        for peer in peers:
            for task in (peer.task, peer.writer, peer.control_reader):
                if not task.done():
                    task.cancel()
            await peer.session.close()
        await asyncio.gather(
            *(task for peer in peers for task in (peer.task, peer.writer, peer.control_reader)),
            return_exceptions=True,
        )
