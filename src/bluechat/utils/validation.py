"""Validation for values received from users or remote peers."""

import unicodedata


def validate_username(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Username must be text")
    normalized = value.strip()
    if not normalized:
        raise ValueError("Username cannot be empty")
    if len(normalized) > 32:
        raise ValueError("Username must be 32 characters or fewer")
    if any(unicodedata.category(char).startswith("C") for char in normalized):
        raise ValueError("Username cannot contain control or formatting characters")
    return normalized
