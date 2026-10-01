"""Authenticated, single-use X25519 session resumption.

Resume credentials are high-entropy, short-lived bearer secrets. They are
never transmitted: possession is proven with transcript-bound HMACs, and fresh
X25519 keys derive new directional AEAD keys for each resumed connection.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import struct
from collections.abc import Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from bluechat.errors import AuthenticationError, SecurityError
from bluechat.security.handshake import EstablishedKeys
from bluechat.security.resume import ResumeRegistry


def _context(room_id: str, session_id: str, participant_id: str) -> bytes:
    result = bytearray()
    for value in (room_id, session_id, participant_id):
        if not isinstance(value, str) or not value or len(value) > 128:
            raise AuthenticationError("Invalid BlueChat resume context")
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise AuthenticationError("Invalid BlueChat resume context") from exc
        result.extend(struct.pack("!H", len(encoded)))
        result.extend(encoded)
    return bytes(result)


def _public(private: X25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _derive(
    private: X25519PrivateKey, peer_public: bytes, token: str, transcript: bytes, proofs: bytes
) -> bytes:
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(peer_public))
        return HKDF(
            algorithm=hashes.SHA256(),
            length=64,
            salt=hashlib.sha256(token.encode("ascii")).digest(),
            info=b"BlueChat resume v1" + hashlib.sha256(transcript + proofs).digest(),
        ).derive(shared)
    except Exception as exc:
        raise SecurityError("Secure session resumption failed") from exc


async def client_resume(
    connection: object,
    token: str,
    room_id: str,
    session_id: str,
    participant_id: str,
) -> EstablishedKeys:
    """Run the client half of resume and return fresh channel keys."""
    if not isinstance(token, str) or not 20 <= len(token) <= 128:
        raise AuthenticationError("Invalid BlueChat resume credential")
    try:
        mac_key = hashlib.sha256(token.encode("ascii", "strict")).digest()
    except UnicodeEncodeError as exc:
        raise AuthenticationError("Invalid BlueChat resume credential") from exc
    context = _context(room_id, session_id, participant_id)
    nonce = secrets.token_bytes(32)
    private = X25519PrivateKey.generate()
    client_public = _public(private)
    client_transcript = b"BlueChat-resume-v1" + context + nonce + client_public
    client_proof = hmac.digest(mac_key, b"client" + client_transcript, "sha256")
    await connection.send(b"BC-RC1" + context + nonce + client_public + client_proof)  # type: ignore[attr-defined]
    response = await connection.receive()  # type: ignore[attr-defined]
    if (
        not isinstance(response, bytes)
        or len(response) != 102
        or not response.startswith(b"BC-RS1")
    ):
        raise AuthenticationError("The host sent an invalid resume response")
    server_nonce, server_public, server_proof = response[6:38], response[38:70], response[70:102]
    transcript = client_transcript + server_nonce + server_public
    expected_server = hmac.digest(mac_key, b"server" + transcript + client_proof, "sha256")
    if not hmac.compare_digest(server_proof, expected_server):
        raise AuthenticationError("The host could not authenticate this resume")
    finish = hmac.digest(mac_key, b"finish" + transcript + client_proof + server_proof, "sha256")
    await connection.send(b"BC-RF1" + finish)  # type: ignore[attr-defined]
    material = _derive(private, server_public, token, transcript, client_proof + server_proof)
    return EstablishedKeys(material[32:], material[:32], hashlib.sha256(transcript).digest())


async def host_resume(
    connection: object,
    registry: ResumeRegistry,
    session_id: str,
    token_lookup: Callable[[str, str], str | None],
    first_message: bytes | None = None,
) -> tuple[EstablishedKeys, str, str]:
    """Run the host half and consume a matching token after client proof."""
    request = first_message or await connection.receive()  # type: ignore[attr-defined]
    if (
        not isinstance(request, bytes)
        or len(request) < 6 + 6 + 64 + 32
        or not request.startswith(b"BC-RC1")
    ):
        raise AuthenticationError("The peer sent an invalid resume request")
    offset = 6
    fields: list[str] = []
    try:
        for _ in range(3):
            (size,) = struct.unpack_from("!H", request, offset)
            offset += 2
            if size < 1 or size > 128 or offset + size > len(request):
                raise ValueError
            fields.append(request[offset : offset + size].decode("ascii"))
            offset += size
    except (ValueError, UnicodeDecodeError, struct.error) as exc:
        raise AuthenticationError("The peer sent an invalid resume context") from exc
    room_id, requested_session, participant_id = fields
    if requested_session != session_id or len(request) != offset + 96:
        raise AuthenticationError("The resume belongs to another BlueChat session")
    client_nonce = request[offset : offset + 32]
    client_public = request[offset + 32 : offset + 64]
    client_proof = request[offset + 64 : offset + 96]
    token = token_lookup(room_id, participant_id)
    if not isinstance(token, str) or not registry.verify(
        token, room_id, participant_id, session_id
    ):
        await connection.send(b"BC-ER1")  # type: ignore[attr-defined]
        raise AuthenticationError("The resume credential is invalid or expired")
    try:
        mac_key = hashlib.sha256(token.encode("ascii", "strict")).digest()
    except UnicodeEncodeError as exc:
        raise AuthenticationError("The resume credential is invalid") from exc
    client_transcript = b"BlueChat-resume-v1" + request[6:offset] + client_nonce + client_public
    expected_client = hmac.digest(mac_key, b"client" + client_transcript, "sha256")
    if not hmac.compare_digest(client_proof, expected_client):
        await connection.send(b"BC-ER1")  # type: ignore[attr-defined]
        raise AuthenticationError("The resume credential could not be verified")
    server_nonce = secrets.token_bytes(32)
    private = X25519PrivateKey.generate()
    server_public = _public(private)
    transcript = client_transcript + server_nonce + server_public
    server_proof = hmac.digest(mac_key, b"server" + transcript + client_proof, "sha256")
    await connection.send(b"BC-RS1" + server_nonce + server_public + server_proof)  # type: ignore[attr-defined]
    finish = await connection.receive()  # type: ignore[attr-defined]
    expected_finish = hmac.digest(
        mac_key, b"finish" + transcript + client_proof + server_proof, "sha256"
    )
    if (
        not isinstance(finish, bytes)
        or len(finish) != 38
        or not finish.startswith(b"BC-RF1")
        or not hmac.compare_digest(finish[6:], expected_finish)
        or not registry.consume(token, room_id, participant_id, session_id)
    ):
        raise AuthenticationError("The resume credential is invalid, expired, or already used")
    material = _derive(private, client_public, token, transcript, client_proof + server_proof)
    return (
        EstablishedKeys(material[:32], material[32:], hashlib.sha256(transcript).digest()),
        room_id,
        participant_id,
    )
