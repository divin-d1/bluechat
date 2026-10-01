"""TOML configuration persistence with safe recovery from damaged files."""

from __future__ import annotations

import os
import logging
import shutil
from pathlib import Path

try:  # Python 3.10 compatibility; tomllib is built in from 3.11 onward.
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    import tomli as _tomllib

from platformdirs import user_config_path, user_data_path

from bluechat.config.models import AppConfig, HistoryPreference
from bluechat.errors import ConfigurationError

logger = logging.getLogger(__name__)


class ConfigManager:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(user_config_path("BlueChat", "BlueChat")) / "config.toml"

    @property
    def default_download_dir(self) -> Path:
        return Path(user_data_path("BlueChat", "BlueChat")) / "Downloads"

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig(download_dir=self.default_download_dir)
        try:
            raw = _tomllib.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("configuration root must be a table")
            allowed = {
                "username",
                "history",
                "download_dir",
                "show_device_addresses",
                "debug_logging",
            }
            unknown = set(raw) - allowed
            if unknown:
                raise ValueError(f"unknown setting: {sorted(unknown)[0]}")
            history = HistoryPreference(raw.get("history", "ask"))
            username = raw.get("username")
            if username is not None and not isinstance(username, str):
                raise ValueError("username must be text")
            download = raw.get("download_dir")
            if download is not None and not isinstance(download, str):
                raise ValueError("download_dir must be a path string")
            for key in ("show_device_addresses", "debug_logging"):
                if key in raw and not isinstance(raw[key], bool):
                    raise ValueError(f"{key} must be true or false")
            return AppConfig(
                username=username,
                history=history,
                download_dir=Path(download).expanduser() if download else self.default_download_dir,
                show_device_addresses=raw.get("show_device_addresses", False),
                debug_logging=raw.get("debug_logging", False),
            )
        except (OSError, _tomllib.TOMLDecodeError, ValueError, TypeError) as exc:
            # Keep the original for manual recovery; don't make config corruption fatal.
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                if not backup.exists():
                    shutil.copy2(self.path, backup)
            except OSError:
                logger.warning("Could not back up damaged config file at %s", self.path)
            logger.warning("Ignoring invalid config at %s (%s)", self.path, type(exc).__name__)
            return AppConfig(download_dir=self.default_download_dir)

    def save(self, config: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        download = (
            str(config.download_dir) if config.download_dir else str(self.default_download_dir)
        )
        contents = (
            f"username = {_toml_string(config.username) if config.username is not None else 'null'}\n"
            f"history = {_toml_string(config.history.value)}\n"
            f"download_dir = {_toml_string(download)}\n"
            f"show_device_addresses = {str(config.show_device_addresses).lower()}\n"
            f"debug_logging = {str(config.debug_logging).lower()}\n"
        )
        # TOML has no null. Omit username until configured.
        if config.username is None:
            contents = contents.replace("username = null\n", "")
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temp_path.write_text(contents, encoding="utf-8")
            os.replace(temp_path, self.path)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise ConfigurationError(f"Could not save configuration: {exc}") from exc


def _toml_string(value: str) -> str:
    # JSON string escaping is compatible with basic TOML strings.
    import json

    return json.dumps(value, ensure_ascii=False)
