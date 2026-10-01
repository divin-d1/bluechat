"""Replay-protected authenticated encryption for session payloads."""

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bluechat.errors import SecurityError


class SecureChannel:
    """AES-256-GCM with monotonic per-direction nonces and sequence binding."""

    def __init__(self, send_key: bytes, receive_key: bytes) -> None:
        if len(send_key) != 32 or len(receive_key) != 32:
            raise ValueError("AES-256-GCM keys must be 32 bytes")
        self._send = AESGCM(send_key)
        self._receive = AESGCM(receive_key)
        self._send_sequence = 0
        self._receive_sequence = 0

    def encrypt(self, plaintext: bytes) -> bytes:
        if not isinstance(plaintext, bytes):
            raise TypeError("plaintext must be bytes")
        if self._send_sequence >= 2**64:
            raise SecurityError("Secure channel sequence exhausted")
        sequence = self._send_sequence
        nonce = b"\x00\x00\x00\x00" + sequence.to_bytes(8, "big")
        ciphertext = self._send.encrypt(nonce, plaintext, sequence.to_bytes(8, "big"))
        self._send_sequence += 1
        return sequence.to_bytes(8, "big") + ciphertext

    def decrypt(self, packet: bytes) -> bytes:
        if not isinstance(packet, bytes) or len(packet) < 24:
            raise SecurityError("Invalid encrypted packet")
        sequence = int.from_bytes(packet[:8], "big")
        if sequence != self._receive_sequence:
            raise SecurityError("Encrypted packet is out of sequence or replayed")
        nonce = b"\x00\x00\x00\x00" + packet[:8]
        try:
            plaintext = self._receive.decrypt(nonce, packet[8:], packet[:8])
        except InvalidTag as exc:
            raise SecurityError("Encrypted packet authentication failed") from exc
        self._receive_sequence += 1
        return plaintext
