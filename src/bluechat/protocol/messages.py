"""Typed JSON control message envelope."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bluechat.protocol.constants import BLUECHAT_PROTOCOL_VERSION

ALLOWED_TYPES = frozenset(
    {
        "HELLO",
        "AUTH",
        "AUTH_OK",
        "AUTH_FAILED",
        "JOIN_REQUEST",
        "JOIN_ACCEPT",
        "JOIN_REJECT",
        "TEXT_MESSAGE",
        "USER_JOINED",
        "USER_LEFT",
        "USER_DISCONNECTED",
        "USER_RECONNECTED",
        "PING",
        "PONG",
        "DISCONNECT",
        "ERROR",
        "FILE_OFFER",
        "FILE_ACCEPT",
        "FILE_REJECT",
        "FILE_START",
        "FILE_CHUNK",
        "FILE_COMPLETE",
        "FILE_FAILED",
        "RECONNECT",
        "RESUME_ACCEPT",
        "RESUME_REJECT",
    }
)


@dataclass(frozen=True, slots=True)
class Message:
    type: str
    payload: dict[str, Any]
    version: int = BLUECHAT_PROTOCOL_VERSION
    message_id: str | None = None

    def __post_init__(self) -> None:
        if self.type not in ALLOWED_TYPES:
            raise ValueError("unsupported message type")
        if self.version != BLUECHAT_PROTOCOL_VERSION:
            raise ValueError("unsupported protocol version")
        if not isinstance(self.payload, dict):
            raise ValueError("message payload must be an object")
