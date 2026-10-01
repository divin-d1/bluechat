"""Room code lifecycle, capacity checks, and host approval."""

from __future__ import annotations

import secrets
import math
import threading
import time
from dataclasses import dataclass, field

from bluechat.errors import (
    ApprovalRejectedError,
    AuthenticationError,
    AuthenticationRateLimitedError,
    RoomExpiredError,
    RoomFullError,
)
from bluechat.security.handshake import generate_room_code, validate_room_code
from bluechat.utils.validation import validate_username

ROOM_CODE_TTL_SECONDS = 300
MAX_PARTICIPANTS = 5  # Includes the host.
AUTH_FAILURE_LIMIT = 5
AUTH_COOLDOWN_SECONDS = 30


@dataclass(slots=True)
class Room:
    host_name: str
    group: bool
    created_at: float = field(default_factory=time.monotonic)
    session_started_at: float = field(default_factory=time.monotonic)
    room_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    session_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    code: str = field(default_factory=generate_room_code)
    participants: set[str] = field(default_factory=set)
    participant_names: dict[str, str] = field(default_factory=dict)
    participant_joined: threading.Event = field(default_factory=threading.Event, repr=False)
    auth_failures: int = 0
    auth_blocked_until: float = 0.0

    def __post_init__(self) -> None:
        self.host_name = validate_username(self.host_name)
        self.participants.add(self.host_name)
        self.participant_names["host"] = self.host_name

    @property
    def expires_at(self) -> float:
        return self.created_at + ROOM_CODE_TTL_SECONDS

    def remaining_seconds(self, now: float | None = None) -> int:
        """Seconds left in the current join window, rounded up for a stable display."""
        current = now if now is not None else time.monotonic()
        return math.ceil(max(0.0, self.expires_at - current))

    def validate_code(self, code: str, now: float | None = None) -> None:
        try:
            supplied = validate_room_code(code)
        except AuthenticationError:
            raise
        if (now if now is not None else time.monotonic()) >= self.expires_at:
            raise RoomExpiredError("The room code has expired")
        if not secrets.compare_digest(supplied, self.code):
            raise AuthenticationError("Room code is incorrect")

    def rotate_code(self, now: float | None = None) -> str:
        previous = self.code
        self.code = generate_room_code()
        while secrets.compare_digest(self.code, previous):
            self.code = generate_room_code()
        self.created_at = now if now is not None else time.monotonic()
        return self.code

    def check_auth_rate_limit(self, now: float | None = None) -> None:
        current = now if now is not None else time.monotonic()
        if current < self.auth_blocked_until:
            raise AuthenticationRateLimitedError(
                "Too many incorrect room-code attempts; wait before retrying"
            )

    def record_auth_failure(self, now: float | None = None) -> None:
        current = now if now is not None else time.monotonic()
        self.auth_failures += 1
        if self.auth_failures >= AUTH_FAILURE_LIMIT:
            self.auth_blocked_until = current + AUTH_COOLDOWN_SECONDS
            self.auth_failures = 0

    def record_auth_success(self) -> None:
        self.auth_failures = 0
        self.auth_blocked_until = 0.0

    def request_join(self, username: str, code: str, *, approved: bool) -> str:
        name = validate_username(username)
        self.validate_code(code)
        if not self.group and len(self.participants) >= 2:
            raise RoomFullError("Private rooms allow exactly two participants")
        if len(self.participants) >= MAX_PARTICIPANTS:
            raise RoomFullError("Room capacity is five participants, including the host")
        if not approved:
            raise ApprovalRejectedError("The host rejected the join request")
        participant_id = secrets.token_urlsafe(16)
        # Usernames need not be unique. IDs remain the stable identity.
        self.participants.add(participant_id)
        self.participant_names[participant_id] = name
        self.participant_joined.set()
        return participant_id

    def remove_participant(self, participant_id: str) -> bool:
        """Remove a non-host participant and report whether it was present."""
        if participant_id == "host" or participant_id not in self.participant_names:
            return False
        self.participants.discard(participant_id)
        self.participant_names.pop(participant_id, None)
        return True

    def list_participants(self) -> tuple[tuple[str, str], ...]:
        """Return stable participant IDs and display names, with host first."""
        return tuple(self.participant_names.items())
