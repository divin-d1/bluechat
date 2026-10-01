"""Concurrent-safe local session facade used by the fake transport and tests."""

from __future__ import annotations

import asyncio
import logging
import queue
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from bluechat.bluetooth.base import Connection
from bluechat.chat.room import Room
from bluechat.errors import ProtocolError
from bluechat.protocol.codec import decode_message, encode_message
from bluechat.protocol.framing import FrameDecoder, encode_frame
from bluechat.protocol.messages import Message
from bluechat.security.encryption import SecureChannel
from bluechat.security.handshake import EstablishedKeys, establish_test_pair
from bluechat.utils.validation import validate_username

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChatMessage:
    sender: str
    text: str
    message_id: str = ""
    sender_id: str = ""
    room_id: str = ""
    timestamp: str = ""


class MemoryTransport:
    """A pair of thread-safe byte streams suitable for tests and demonstrations."""

    def __init__(self) -> None:
        self._incoming: queue.Queue[bytes | None] = queue.Queue()
        self._peer: MemoryTransport | None = None
        self._closed = False
        self._pending = bytearray()
        self._send_lock = threading.Lock()
        self._receive_lock = threading.Lock()

    @classmethod
    def pair(cls) -> tuple[MemoryTransport, MemoryTransport]:
        left, right = cls(), cls()
        left._peer, right._peer = right, left
        return left, right

    def sendall(self, data: bytes) -> None:
        if self._closed or self._peer is None or self._peer._closed:
            raise ConnectionError("transport is closed")
        # Deliberately split writes to exercise the same framing assumption as a stream.
        midpoint = max(1, len(data) // 2)
        with self._send_lock:
            self._peer._incoming.put(data[:midpoint])
            if midpoint < len(data):
                self._peer._incoming.put(data[midpoint:])

    def recv(self, size: int) -> bytes:
        if size <= 0:
            raise ValueError("size must be positive")
        with self._receive_lock:
            while not self._pending:
                data = self._incoming.get()
                if data is None:
                    return b""
                self._pending.extend(data)
            result = bytes(self._pending[:size])
            del self._pending[:size]
            return result

    def close(self) -> None:
        self._closed = True
        if self._peer is not None:
            self._peer._incoming.put(None)


class ChatSession:
    def __init__(
        self, username: str, peer_username: str, transport: MemoryTransport, channel: SecureChannel
    ) -> None:
        self.username = validate_username(username)
        self.peer_username = validate_username(peer_username)
        self.transport = transport
        self.channel = channel
        self._decoder = FrameDecoder()
        self._messages: queue.Queue[ChatMessage | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, name="bluechat-reader", daemon=True)
        self._closed = threading.Event()
        self._reader.start()

    def send_text(self, text: str) -> None:
        if not isinstance(text, str) or not text.strip() or len(text) > 16_000:
            raise ValueError("Message must contain 1 to 16000 characters")
        raw = encode_message(Message("TEXT_MESSAGE", {"sender": self.username, "text": text}))
        self.transport.sendall(encode_frame(self.channel.encrypt(raw)))

    def receive(self, timeout: float | None = None) -> ChatMessage:
        item = self._messages.get(timeout=timeout)
        if item is None:
            raise ConnectionError("session disconnected")
        return item

    def close(self) -> None:
        if not self._closed.is_set():
            self._closed.set()
            self.transport.close()
            self._messages.put(None)

    def _read_loop(self) -> None:
        try:
            while not self._closed.is_set():
                chunk = self.transport.recv(4096)
                if not chunk:
                    break
                for frame in self._decoder.feed(chunk):
                    message = decode_message(self.channel.decrypt(frame))
                    if message.type == "DISCONNECT":
                        return
                    if message.type != "TEXT_MESSAGE":
                        continue
                    sender, text = message.payload.get("sender"), message.payload.get("text")
                    if (
                        sender != self.peer_username
                        or not isinstance(text, str)
                        or len(text) > 16_000
                    ):
                        continue
                    self._messages.put(ChatMessage(sender, text))
        except (ConnectionError, OSError):
            logger.debug("Memory chat transport disconnected")
        except Exception as exc:
            # Never include packet contents, keys, or user text in this diagnostic.
            logger.warning(
                "Closing memory session after invalid protocol data (%s)", type(exc).__name__
            )
        finally:
            self._closed.set()
            self._messages.put(None)


def make_secure_sessions(
    code: str, left_user: str, right_user: str
) -> tuple[ChatSession, ChatSession]:
    left_transport, right_transport = MemoryTransport.pair()
    left_keys, right_keys = establish_test_pair(code)
    return (
        ChatSession(
            left_user,
            right_user,
            left_transport,
            SecureChannel(left_keys.send_key, left_keys.receive_key),
        ),
        ChatSession(
            right_user,
            left_user,
            right_transport,
            SecureChannel(right_keys.send_key, right_keys.receive_key),
        ),
    )


def approve_and_join(
    room: Room, username: str, code: str, approved: bool
) -> tuple[str, ChatSession, ChatSession]:
    participant_id = room.request_join(username, code, approved=approved)
    host, guest = make_secure_sessions(code, room.host_name, username)
    return participant_id, host, guest


class AsyncChatSession:
    """Application protocol and encryption layered above a Bluetooth Connection."""

    def __init__(
        self,
        username: str,
        peer_username: str | None,
        connection: Connection,
        keys: EstablishedKeys,
        *,
        participant_id: str | None = None,
        room_id: str = "",
        allow_remote_senders: bool = False,
    ) -> None:
        self.username = validate_username(username)
        self.peer_username = validate_username(peer_username) if peer_username else None
        self.connection = connection
        self.channel = SecureChannel(keys.send_key, keys.receive_key)
        self.participant_id = participant_id or secrets.token_urlsafe(16)
        self.room_id = room_id
        self.allow_remote_senders = allow_remote_senders
        self._messages: asyncio.Queue[ChatMessage | None] = asyncio.Queue(maxsize=256)
        self._controls: asyncio.Queue[Message | None] = asyncio.Queue(maxsize=64)
        self._transfer_waiters: dict[str, asyncio.Queue[Message]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._decoder = FrameDecoder()
        self._closed = False
        self._send_lock = asyncio.Lock()

    def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop(), name="bluechat-async-reader")

    async def send_text(self, text: str) -> None:
        if not isinstance(text, str) or not text.strip() or len(text) > 16_000:
            raise ValueError("Message must contain 1 to 16000 characters")
        raw = encode_message(
            Message(
                "TEXT_MESSAGE",
                {
                    "sender": self.username,
                    "sender_id": self.participant_id,
                    "room_id": self.room_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "text": text,
                },
                message_id=secrets.token_urlsafe(16),
            )
        )
        await self._send_raw(raw)

    async def send_chat_message(self, message: ChatMessage) -> None:
        """Forward a validated room message without changing its original identity."""
        if len(message.text) > 16_000 or not message.sender_id or not message.message_id:
            raise ProtocolError("Cannot route an invalid chat message")
        raw = encode_message(
            Message(
                "TEXT_MESSAGE",
                {
                    "sender": message.sender,
                    "sender_id": message.sender_id,
                    "room_id": message.room_id,
                    "timestamp": message.timestamp,
                    "text": message.text,
                },
                message_id=message.message_id,
            )
        )
        await self._send_raw(raw)

    async def send_control(self, kind: str, payload: dict[str, object] | None = None) -> None:
        raw = encode_message(Message(kind, payload or {}))
        await self._send_raw(raw)

    async def _send_raw(self, raw: bytes) -> None:
        async with self._send_lock:
            if self._closed:
                raise ConnectionError("chat session is closed")
            await self.connection.send(encode_frame(self.channel.encrypt(raw)))

    async def receive(self) -> ChatMessage:
        item = await self._messages.get()
        if item is None:
            raise ConnectionError("chat session disconnected")
        return item

    async def receive_control(
        self, expected: str | None = None, *, timeout: float | None = None
    ) -> Message:
        message = (
            await asyncio.wait_for(self._controls.get(), timeout)
            if timeout is not None
            else await self._controls.get()
        )
        if message is None:
            raise ConnectionError("chat session disconnected")
        if expected is not None and message.type != expected:
            raise ProtocolError(f"Expected {expected}, received {message.type}")
        return message

    def open_transfer(self, transfer_id: str) -> None:
        """Register a transfer-specific control queue before offering/accepting."""
        if not transfer_id or transfer_id in self._transfer_waiters:
            raise ProtocolError("Invalid or duplicate transfer ID")
        self._transfer_waiters[transfer_id] = asyncio.Queue(maxsize=8)

    async def receive_transfer_control(
        self,
        transfer_id: str,
        expected: str | None = None,
        *,
        timeout: float | None = None,
    ) -> Message:
        queue = self._transfer_waiters.get(transfer_id)
        if queue is None:
            raise ProtocolError("No active transfer matches this control message")
        message = (
            await asyncio.wait_for(queue.get(), timeout)
            if timeout is not None
            else await queue.get()
        )
        if expected is not None and message.type != expected:
            raise ProtocolError(f"Expected {expected}, received {message.type}")
        return message

    def close_transfer(self, transfer_id: str) -> None:
        self._transfer_waiters.pop(transfer_id, None)

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                packet = await self.connection.receive()
                for frame in self._decoder.feed(packet):
                    message = decode_message(self.channel.decrypt(frame))
                    if message.type != "TEXT_MESSAGE":
                        transfer_id = message.payload.get("transfer_id")
                        transfer_queue = (
                            self._transfer_waiters.get(transfer_id)
                            if isinstance(transfer_id, str)
                            else None
                        )
                        if transfer_queue is not None:
                            try:
                                transfer_queue.put_nowait(message)
                            except asyncio.QueueFull as exc:
                                raise ProtocolError(
                                    "Incoming transfer control queue is full"
                                ) from exc
                            continue
                        try:
                            self._controls.put_nowait(message)
                        except asyncio.QueueFull as exc:
                            raise ProtocolError("Incoming control queue is full") from exc
                        continue
                    sender, body = message.payload.get("sender"), message.payload.get("text")
                    if (
                        not isinstance(sender, str)
                        or (
                            not self.allow_remote_senders
                            and self.peer_username is not None
                            and sender != self.peer_username
                        )
                        or not isinstance(body, str)
                        or len(body) > 16_000
                    ):
                        raise ProtocolError("Invalid chat message identity or text")
                    sender_id = message.payload.get("sender_id", "")
                    room_id = message.payload.get("room_id", self.room_id)
                    timestamp = message.payload.get("timestamp", "")
                    if (
                        not isinstance(sender_id, str)
                        or len(sender_id) > 128
                        or not isinstance(room_id, str)
                        or (self.room_id and room_id != self.room_id)
                        or not isinstance(timestamp, str)
                        or len(timestamp) > 64
                    ):
                        raise ProtocolError("Invalid chat message metadata")
                    if self.peer_username is None:
                        self.peer_username = validate_username(sender)
                    try:
                        self._messages.put_nowait(
                            ChatMessage(
                                sender,
                                body,
                                message.message_id or "",
                                sender_id,
                                room_id,
                                timestamp,
                            )
                        )
                    except asyncio.QueueFull as exc:
                        raise ProtocolError("Incoming chat message queue is full") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Closing chat session after transport/protocol failure (%s)", type(exc).__name__
            )
        finally:
            self._closed = True
            try:
                self._messages.put_nowait(None)
            except asyncio.QueueFull:
                # Make room for the disconnect marker so a waiting UI cannot hang.
                self._messages.get_nowait()
                self._messages.put_nowait(None)
            try:
                self._controls.put_nowait(None)
            except asyncio.QueueFull:
                self._controls.get_nowait()
                self._controls.put_nowait(None)

    async def close(self) -> None:
        self._closed = True
        await self.connection.close()
        if self._reader is not None and self._reader is not asyncio.current_task():
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
