"""Short-lived, single-use in-memory resume credentials bound to a room peer."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _ResumeEntry:
    room_id: str
    participant_id: str
    session_id: str
    expires_at: float | None


class ResumeRegistry:
    """Issue opaque bearer tokens that expire and can be consumed only once.

    Tokens are intentionally process-memory-only; restarting the host invalidates
    all outstanding resumes. The token itself is never retained by the registry.
    """

    def __init__(self, *, ttl_seconds: int = 30, clock=time.monotonic) -> None:
        if ttl_seconds < 1 or ttl_seconds > 30:
            raise ValueError("resume TTL must be between 1 and 30 seconds")
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: dict[bytes, _ResumeEntry] = {}

    def issue(
        self,
        room_id: str,
        participant_id: str,
        session_id: str = "",
        *,
        deferred_expiry: bool = False,
    ) -> str:
        if (
            not isinstance(room_id, str)
            or not isinstance(participant_id, str)
            or not room_id
            or not participant_id
            or len(room_id) > 128
            or len(participant_id) > 128
            or not isinstance(session_id, str)
            or len(session_id) > 128
        ):
            raise ValueError("resume credentials require room and participant IDs")
        self.purge()
        if len(self._entries) >= 5:
            raise ValueError("the room has no free resume-token slots")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("ascii")).digest()
        self._entries[digest] = _ResumeEntry(
            room_id,
            participant_id,
            session_id,
            None if deferred_expiry else self._clock() + self.ttl_seconds,
        )
        return token

    def suspend(self, token: str, room_id: str, participant_id: str, session_id: str) -> bool:
        """Start the bounded reconnect TTL for a credential issued to a live peer."""
        digest = self._digest(token)
        if digest is None:
            return False
        entry = self._entries.get(digest)
        if (
            entry is None
            or entry.expires_at is not None
            or not hmac.compare_digest(entry.room_id, room_id)
            or not hmac.compare_digest(entry.participant_id, participant_id)
            or not hmac.compare_digest(entry.session_id, session_id)
        ):
            return False
        self._entries[digest] = _ResumeEntry(
            entry.room_id, entry.participant_id, entry.session_id, self._clock() + self.ttl_seconds
        )
        return True

    def verify(self, token: str, room_id: str, participant_id: str, session_id: str = "") -> bool:
        """Check a token binding without consuming it; use only before proof validation."""
        if not all(isinstance(value, str) for value in (room_id, participant_id, session_id)):
            return False
        if any(len(value) > 128 for value in (room_id, participant_id, session_id)):
            return False
        entry = self._lookup(token)
        return bool(
            entry
            and entry.expires_at is not None
            and self._clock() < entry.expires_at
            and hmac.compare_digest(entry.room_id, room_id)
            and hmac.compare_digest(entry.participant_id, participant_id)
            and hmac.compare_digest(entry.session_id, session_id)
        )

    def consume(self, token: str, room_id: str, participant_id: str, session_id: str = "") -> bool:
        """Validate and invalidate a matching token, including expired tokens."""
        if (
            not isinstance(token, str)
            or len(token) > 128
            or not isinstance(room_id, str)
            or not isinstance(participant_id, str)
            or len(room_id) > 128
            or len(participant_id) > 128
        ):
            return False
        digest = self._digest(token)
        if digest is None:
            return False
        entry = self._entries.pop(digest, None)
        if entry is None:
            return False
        return (
            self._clock() < entry.expires_at
            if entry is not None and entry.expires_at is not None
            else False
        ) and (
            hmac.compare_digest(entry.room_id, room_id)
            and hmac.compare_digest(entry.participant_id, participant_id)
            and hmac.compare_digest(entry.session_id, session_id)
        )

    def _lookup(self, token: str) -> _ResumeEntry | None:
        digest = self._digest(token)
        return self._entries.get(digest) if digest is not None else None

    @staticmethod
    def _digest(token: str) -> bytes | None:
        if not isinstance(token, str) or len(token) > 128:
            return None
        try:
            return hashlib.sha256(token.encode("ascii")).digest()
        except UnicodeEncodeError:
            return None

    def purge(self) -> int:
        now = self._clock()
        expired = [
            digest
            for digest, entry in self._entries.items()
            if entry.expires_at is not None and now >= entry.expires_at
        ]
        for digest in expired:
            self._entries.pop(digest, None)
        return len(expired)
