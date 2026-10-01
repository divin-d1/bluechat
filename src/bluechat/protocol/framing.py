"""Length-prefixed framing that handles fragmented and coalesced stream reads."""

from __future__ import annotations

import struct

from bluechat.errors import ProtocolError
from bluechat.protocol.constants import MAX_FRAME_SIZE

_HEADER = struct.Struct("!I")


def encode_frame(payload: bytes) -> bytes:
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_FRAME_SIZE:
        raise ProtocolError("Frame must contain 1 byte to 1 MiB")
    return _HEADER.pack(len(payload)) + payload


class FrameDecoder:
    """Incrementally decode frames; invalid lengths poison the decoder."""

    def __init__(self, max_frame_size: int = MAX_FRAME_SIZE) -> None:
        if not 1 <= max_frame_size <= MAX_FRAME_SIZE:
            raise ValueError("invalid frame limit")
        self._buffer = bytearray()
        self.max_frame_size = max_frame_size
        self._failed = False

    def feed(self, data: bytes) -> list[bytes]:
        if self._failed:
            raise ProtocolError("Frame decoder is unusable after an earlier protocol error")
        if not isinstance(data, bytes):
            self._failed = True
            raise ProtocolError("Frame input must be bytes")
        if len(self._buffer) + len(data) > self.max_frame_size + _HEADER.size:
            self._failed = True
            self._buffer.clear()
            raise ProtocolError("Stream read exceeds the configured frame buffer limit")
        self._buffer.extend(data)
        frames: list[bytes] = []
        try:
            while len(self._buffer) >= _HEADER.size:
                (size,) = _HEADER.unpack(self._buffer[: _HEADER.size])
                if size == 0 or size > self.max_frame_size:
                    raise ProtocolError("Invalid or oversized frame length")
                if len(self._buffer) < _HEADER.size + size:
                    break
                start = _HEADER.size
                frames.append(bytes(self._buffer[start : start + size]))
                del self._buffer[: start + size]
            return frames
        except ProtocolError:
            self._failed = True
            self._buffer.clear()
            raise
