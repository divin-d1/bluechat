"""Platform backend selection and role-aware availability checks."""

from __future__ import annotations

import asyncio
import sys

from bluechat.bluetooth.base import BluetoothTransport
from bluechat.errors import BluetoothCapabilityError, BluetoothUnavailableError

BLUETOOTH_PROBE_TIMEOUT = 10.0


class BluetoothManager:
    def __init__(self, transport: BluetoothTransport | None = None) -> None:
        self.transport = transport or _platform_transport()

    async def require_ready(self, *, role: str = "client") -> None:
        if role not in {"client", "host"}:
            raise ValueError("Bluetooth role must be client or host")
        needed = (
            self.transport.capabilities.discovery
            if role == "client"
            else self.transport.capabilities.hosting
        )
        if not needed:
            if role == "host":
                raise BluetoothCapabilityError(
                    f"This platform can discover and join BlueChat rooms, but hosting is not implemented yet. "
                    f"{_hosting_guidance()}"
                )
            raise BluetoothCapabilityError(
                "This platform backend does not support Bluetooth discovery"
            )
        try:
            available = await asyncio.wait_for(
                self.transport.is_available(), timeout=BLUETOOTH_PROBE_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            raise BluetoothUnavailableError(
                "Bluetooth adapter detection timed out. Check system Bluetooth settings and retry."
            ) from exc
        if not available:
            raise BluetoothUnavailableError(
                "No usable Bluetooth adapter is available. Check the adapter and system Bluetooth service."
            )
        try:
            enabled = await asyncio.wait_for(
                self.transport.is_enabled(), timeout=BLUETOOTH_PROBE_TIMEOUT
            )
        except asyncio.TimeoutError as exc:
            raise BluetoothUnavailableError(
                "Bluetooth state detection timed out. Check system Bluetooth settings and retry."
            ) from exc
        if not enabled:
            raise BluetoothUnavailableError(
                "Bluetooth is turned off. Enable Bluetooth in system settings."
            )


def _platform_transport() -> BluetoothTransport:
    if sys.platform.startswith("linux"):
        from bluechat.bluetooth.linux import LinuxBluetoothBackend

        return LinuxBluetoothBackend()
    if sys.platform == "win32":
        from bluechat.bluetooth.windows import WindowsBluetoothBackend

        return WindowsBluetoothBackend()
    if sys.platform == "darwin":
        from bluechat.bluetooth.macos import MacOSBluetoothBackend

        return MacOSBluetoothBackend()
    from bluechat.errors import BluetoothUnavailableError

    raise BluetoothUnavailableError(f"Bluetooth is not supported on platform {sys.platform!r}")


def _hosting_guidance() -> str:
    if sys.platform == "win32":
        return (
            "Windows hosting requires a BLE-capable adapter and peripheral-role support; "
            "check the adapter driver and Windows Bluetooth permissions."
        )
    if sys.platform == "darwin":
        return "macOS hosting uses CoreBluetooth; check the permission and adapter status above."
    return "Use a platform backend with BLE GATT peripheral support."
