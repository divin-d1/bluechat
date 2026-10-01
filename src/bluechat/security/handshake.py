"""Room-code authentication using the maintained SPAKE2 PAKE implementation."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from spake2 import SPAKE2_A, SPAKE2_B

from bluechat.errors import AuthenticationError, SecurityError

ROOM_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_CODE_LENGTH = 6
_IDENTITY_A = b"BlueChat joining participant"
_IDENTITY_B = b"BlueChat room host"
_PAKE_DOMAIN = b"BlueChat SPAKE2 handshake v2"


def generate_room_code() -> str:
    return "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(ROOM_CODE_LENGTH))


def validate_room_code(code: str) -> str:
    if not isinstance(code, str) or len(code) != ROOM_CODE_LENGTH:
        raise AuthenticationError("Room code must be six characters")
    normalized = code.upper()
    if any(char not in ROOM_CODE_ALPHABET for char in normalized):
        raise AuthenticationError("Room code contains unsupported characters")
    return normalized


def _derive_pake_material(shared: bytes, transcript: bytes) -> bytes:
    if not isinstance(shared, bytes) or len(shared) < 16:
        raise SecurityError("The password-authenticated exchange returned invalid key material")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=64,
        salt=hashlib.sha256(transcript).digest(),
        info=b"BlueChat directional session keys v2",
    ).derive(shared)


def _test_keys(shared: bytes, transcript: bytes) -> tuple["EstablishedKeys", "EstablishedKeys"]:
    material = _derive_pake_material(shared, transcript)
    transcript_id = hashlib.sha256(transcript).digest()
    return (
        EstablishedKeys(material[:32], material[32:], transcript_id),
        EstablishedKeys(material[32:], material[:32], transcript_id),
    )


@dataclass(frozen=True, slots=True)
class EstablishedKeys:
    send_key: bytes
    receive_key: bytes
    transcript_id: bytes


def establish_test_pair(
    code: str, peer_code: str | None = None
) -> tuple[EstablishedKeys, EstablishedKeys]:
    """Create test-only secure directions through the SPAKE2 implementation."""
    normalized = validate_room_code(code).encode("ascii")
    peer_normalized = validate_room_code(peer_code if peer_code is not None else code).encode(
        "ascii"
    )
    try:
        client = SPAKE2_A(normalized, idA=_IDENTITY_A, idB=_IDENTITY_B)
        host = SPAKE2_B(peer_normalized, idA=_IDENTITY_A, idB=_IDENTITY_B)
        client_message = client.start()
        host_message = host.start()
        client_key = client.finish(host_message)
        host_key = host.finish(client_message)
    except Exception as exc:
        raise AuthenticationError("Room authentication failed") from exc
    if not hmac.compare_digest(client_key, host_key):
        raise AuthenticationError("Room authentication failed")
    transcript = _PAKE_DOMAIN + client_message + host_message
    return _test_keys(host_key, transcript)[0], _test_keys(client_key, transcript)[1]


async def perform_client_handshake(connection: object, code: str) -> EstablishedKeys:
    """Authenticate with SPAKE2, confirm the transcript, and derive fresh keys."""
    password = validate_room_code(code).encode("ascii")
    try:
        pake = SPAKE2_A(password, idA=_IDENTITY_A, idB=_IDENTITY_B)
        client_message = pake.start()
        if len(client_message) > 512 or not client_message.startswith(b"A"):
            raise SecurityError("SPAKE2 returned an invalid client message")
        await connection.send(b"BC-PA2" + client_message)  # type: ignore[attr-defined]
        response = await connection.receive()  # type: ignore[attr-defined]
        if (
            not isinstance(response, bytes)
            or len(response) < 7
            or not response.startswith(b"BC-PB2")
        ):
            raise AuthenticationError("The host sent an invalid authentication response")
        host_message = b"B" + response[6:]
        shared = pake.finish(host_message)
        transcript = _PAKE_DOMAIN + client_message + host_message
        client_proof = hmac.digest(shared, b"client confirmation" + transcript, "sha256")
        await connection.send(b"BC-PC2" + client_proof)  # type: ignore[attr-defined]
        confirmation = await connection.receive()  # type: ignore[attr-defined]
        expected = hmac.digest(shared, b"host confirmation" + transcript + client_proof, "sha256")
        if (
            not isinstance(confirmation, bytes)
            or len(confirmation) != 38
            or not confirmation.startswith(b"BC-OK2")
            or not hmac.compare_digest(confirmation[6:], expected)
        ):
            raise AuthenticationError("Room code is incorrect or the host could not authenticate")
        material = _derive_pake_material(shared, transcript)
        return EstablishedKeys(material[32:], material[:32], hashlib.sha256(transcript).digest())
    except AuthenticationError:
        raise
    except Exception as exc:
        raise SecurityError("Secure room authentication failed") from exc


async def perform_host_handshake(connection: object, code: str) -> EstablishedKeys:
    """Verify a joining participant's PAKE proof before host approval."""
    password = validate_room_code(code).encode("ascii")
    try:
        request = await connection.receive()  # type: ignore[attr-defined]
        if (
            not isinstance(request, bytes)
            or len(request) < 7
            or len(request) > 519
            or not request.startswith(b"BC-PA2")
            or not request[6:7] == b"A"
        ):
            raise AuthenticationError("The peer sent an invalid authentication request")
        client_message = request[6:]
        pake = SPAKE2_B(password, idA=_IDENTITY_A, idB=_IDENTITY_B)
        host_message = pake.start()
        if not host_message.startswith(b"B") or len(host_message) > 512:
            raise SecurityError("SPAKE2 returned an invalid host message")
        shared = pake.finish(client_message)
        transcript = _PAKE_DOMAIN + client_message + host_message
        await connection.send(b"BC-PB2" + host_message[1:])  # type: ignore[attr-defined]
        proof = await connection.receive()  # type: ignore[attr-defined]
        expected_client = hmac.digest(shared, b"client confirmation" + transcript, "sha256")
        if (
            not isinstance(proof, bytes)
            or len(proof) != 38
            or not proof.startswith(b"BC-PC2")
            or not hmac.compare_digest(proof[6:], expected_client)
        ):
            await connection.send(b"BC-ER1")  # type: ignore[attr-defined]
            raise AuthenticationError("Room code is incorrect")
        server_proof = hmac.digest(shared, b"host confirmation" + transcript + proof[6:], "sha256")
        await connection.send(b"BC-OK2" + server_proof)  # type: ignore[attr-defined]
        material = _derive_pake_material(shared, transcript)
        return EstablishedKeys(material[:32], material[32:], hashlib.sha256(transcript).digest())
    except AuthenticationError:
        raise
    except Exception as exc:
        raise SecurityError("Secure room authentication failed") from exc
