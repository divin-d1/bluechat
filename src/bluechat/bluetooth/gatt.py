"""BlueChat's transport-neutral BLE GATT identifiers and bounded fragmentation."""

from __future__ import annotations

import secrets
import struct
from collections import OrderedDict

from bluechat.errors import ProtocolError

# A 128-bit UUID identifies BlueChat, so clients can filter scans. The server
# exposes one write-with-response RX and one notify TX characteristic. Control,
# chat, and future file records remain protocol messages over this byte pipe.
BLUECHAT_SERVICE_UUID = "a64e2f20-8d8a-4f1b-a5cc-5d64b5c0a100"
BLUECHAT_RX_UUID = "a64e2f20-8d8a-4f1b-a5cc-5d64b5c0a101"
BLUECHAT_TX_UUID = "a64e2f20-8d8a-4f1b-a5cc-5d64b5c0a102"
BLUECHAT_CONTROL_UUID = "a64e2f20-8d8a-4f1b-a5cc-5d64b5c0a103"

FRAGMENT_MAGIC = b"BC"
FRAGMENT_HEADER = struct.Struct("!2sIHH")
MAX_REASSEMBLED_SIZE = 1_048_576
MAX_FRAGMENTS = 65_535


def fragment_packet(
    data: bytes, *, fragment_size: int = 10, frame_id: int | None = None
) -> list[bytes]:
    """Split one packet into ATT-MTU-conservative chunks (20 ATT bytes at MTU 23)."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_REASSEMBLED_SIZE:
        raise ProtocolError("Bluetooth packet must contain 1 byte to 1 MiB")
    if not 1 <= fragment_size <= 512 - FRAGMENT_HEADER.size:
        raise ValueError("fragment size is outside the supported ATT range")
    count = (len(data) + fragment_size - 1) // fragment_size
    if count > MAX_FRAGMENTS:
        raise ProtocolError("Bluetooth packet requires too many fragments")
    identifier = frame_id if frame_id is not None else secrets.randbits(32)
    if not 0 <= identifier <= 0xFFFFFFFF:
        raise ValueError("frame ID must fit in 32 bits")
    return [
        FRAGMENT_HEADER.pack(FRAGMENT_MAGIC, identifier, index, count)
        + data[index * fragment_size : (index + 1) * fragment_size]
        for index in range(count)
    ]


class FragmentReassembler:
    """Bounded out-of-order fragment reassembly with a small concurrent-frame cap."""

    def __init__(self, *, max_frames: int = 4, max_size: int = MAX_REASSEMBLED_SIZE) -> None:
        if max_frames < 1 or not 1 <= max_size <= MAX_REASSEMBLED_SIZE:
            raise ValueError("invalid GATT reassembly limits")
        self.max_frames = max_frames
        self.max_size = max_size
        self._frames: OrderedDict[int, tuple[int, dict[int, bytes], int]] = OrderedDict()

    def feed(self, fragment: bytes) -> bytes | None:
        if not isinstance(fragment, bytes):
            raise ProtocolError("Bluetooth fragment must be bytes")
        if len(fragment) <= FRAGMENT_HEADER.size:
            raise ProtocolError("Bluetooth fragment is too short")
        magic, frame_id, index, total = FRAGMENT_HEADER.unpack_from(fragment)
        payload = fragment[FRAGMENT_HEADER.size :]
        if magic != FRAGMENT_MAGIC or total == 0 or index >= total or total > MAX_FRAGMENTS:
            raise ProtocolError("Invalid Bluetooth fragment header")
        current = self._frames.get(frame_id)
        if current is None:
            while len(self._frames) >= self.max_frames:
                self._frames.popitem(last=False)
            current = (total, {}, 0)
            self._frames[frame_id] = current
        expected_total, parts, size = current
        if total != expected_total:
            del self._frames[frame_id]
            raise ProtocolError("Inconsistent Bluetooth fragment count")
        previous = parts.get(index)
        if previous is not None:
            if previous != payload:
                del self._frames[frame_id]
                raise ProtocolError("Conflicting duplicate Bluetooth fragment")
            return None
        size += len(payload)
        if size > self.max_size:
            del self._frames[frame_id]
            raise ProtocolError("Reassembled Bluetooth packet is too large")
        parts[index] = payload
        self._frames[frame_id] = (total, parts, size)
        if len(parts) != total:
            return None
        del self._frames[frame_id]
        return b"".join(parts[position] for position in range(total))
