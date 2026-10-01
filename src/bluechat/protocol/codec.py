"""Strict JSON encoding and validation for protocol control messages."""

import json
from typing import Any

from bluechat.errors import ProtocolError
from bluechat.protocol.constants import BLUECHAT_PROTOCOL_VERSION, MAX_METADATA_SIZE
from bluechat.protocol.messages import ALLOWED_TYPES, Message


def encode_message(message: Message) -> bytes:
    obj = {"version": message.version, "type": message.type, "payload": message.payload}
    if message.message_id is not None:
        obj["message_id"] = message.message_id
    try:
        raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolError("Message contains invalid data") from exc
    if len(raw) > MAX_METADATA_SIZE:
        raise ProtocolError("Message metadata is too large")
    return raw


def decode_message(raw: bytes) -> Message:
    if not isinstance(raw, bytes) or len(raw) > MAX_METADATA_SIZE:
        raise ProtocolError("Invalid or oversized protocol message")
    try:
        obj: Any = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolError("Malformed protocol message") from exc
    if not isinstance(obj, dict) or set(obj) - {"version", "type", "payload", "message_id"}:
        raise ProtocolError("Unexpected protocol fields")
    if obj.get("version") != BLUECHAT_PROTOCOL_VERSION:
        raise ProtocolError("Unsupported protocol version")
    if obj.get("type") not in ALLOWED_TYPES:
        raise ProtocolError("Unsupported message type")
    payload = obj.get("payload")
    message_id = obj.get("message_id")
    if not isinstance(payload, dict) or (
        message_id is not None and not isinstance(message_id, str)
    ):
        raise ProtocolError("Invalid protocol message fields")
    try:
        return Message(
            type=obj["type"], payload=payload, version=obj["version"], message_id=message_id
        )
    except ValueError as exc:
        raise ProtocolError(str(exc)) from exc


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")
