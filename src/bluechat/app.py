"""Public application facade shared by CLI and importers."""

from __future__ import annotations

from pathlib import Path

from bluechat.bluetooth.manager import BluetoothManager
from bluechat.chat.room import Room
from bluechat.chat.session import ChatSession, approve_and_join
from bluechat.config.manager import ConfigManager
from bluechat.config.models import AppConfig


class BlueChat:
    """BlueChat services with injectable configuration and Bluetooth backend."""

    def __init__(
        self,
        username: str | None = None,
        *,
        config_manager: ConfigManager | None = None,
        bluetooth: BluetoothManager | None = None,
    ) -> None:
        self.config_manager = config_manager or ConfigManager()
        self.config = self.config_manager.load()
        if username is not None:
            self.config = AppConfig(
                username=username,
                history=self.config.history,
                download_dir=self.config.download_dir,
                show_device_addresses=self.config.show_device_addresses,
                debug_logging=self.config.debug_logging,
            )
        self.bluetooth = bluetooth or BluetoothManager()

    @property
    def username(self) -> str | None:
        return self.config.username

    def set_username(self, username: str) -> None:
        updated = AppConfig(
            username=username,
            history=self.config.history,
            download_dir=self.config.download_dir,
            show_device_addresses=self.config.show_device_addresses,
            debug_logging=self.config.debug_logging,
        )
        self.config_manager.save(updated)
        self.config = updated

    def create_room(self, *, group: bool = False) -> Room:
        if not self.username:
            raise ValueError("Choose a username before creating a room")
        return Room(host_name=self.username, group=group)

    def join_memory_room(
        self, room: Room, code: str, *, username: str | None = None, approved: bool
    ) -> tuple[str, ChatSession, ChatSession]:
        guest_name = username or self.username
        if not guest_name:
            raise ValueError("Choose a username before joining a room")
        return approve_and_join(room, guest_name, code, approved)

    def set_download_dir(self, path: Path) -> None:
        updated = AppConfig(
            username=self.username,
            history=self.config.history,
            download_dir=path.expanduser(),
            show_device_addresses=self.config.show_device_addresses,
            debug_logging=self.config.debug_logging,
        )
        self.config_manager.save(updated)
        self.config = updated

    def update_config(self, **changes: object) -> None:
        """Validate and persist changed settings without mutating the live model."""
        values: dict[str, object] = {
            "username": self.config.username,
            "history": self.config.history,
            "download_dir": self.config.download_dir,
            "show_device_addresses": self.config.show_device_addresses,
            "debug_logging": self.config.debug_logging,
        }
        unknown = set(changes) - values.keys()
        if unknown:
            raise ValueError(f"Unknown setting: {sorted(unknown)[0]}")
        values.update(changes)
        updated = AppConfig(**values)  # type: ignore[arg-type]
        self.config_manager.save(updated)
        self.config = updated
