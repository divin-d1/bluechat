"""Validated configuration data models."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from bluechat.errors import ConfigurationError
from bluechat.utils.validation import validate_username


class HistoryPreference(str, Enum):
    ASK = "ask"
    ALWAYS = "always"
    NEVER = "never"


@dataclass(frozen=True, slots=True)
class AppConfig:
    username: str | None = None
    history: HistoryPreference = HistoryPreference.ASK
    download_dir: Path | None = None
    show_device_addresses: bool = False
    debug_logging: bool = False

    def __post_init__(self) -> None:
        if self.username is not None:
            try:
                validate_username(self.username)
            except ValueError as exc:
                raise ConfigurationError(str(exc)) from exc
        if not isinstance(self.history, HistoryPreference):
            raise ConfigurationError("history must be ask, always, or never")
        if self.download_dir is not None and not isinstance(self.download_dir, Path):
            raise ConfigurationError("download_dir must be a filesystem path")
