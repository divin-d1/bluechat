"""Windows BLE central/client through Bleak and GATT hosting through WinRT."""

from typing import Any

from bluechat.bluetooth.base import BluetoothCapabilities, Connection
from bluechat.bluetooth.bleak_central import BleakCentralTransport
from bluechat.bluetooth.windows_peripheral import WindowsGattPeripheral
from bluechat.errors import (
    BluetoothPermissionError,
    BluetoothUnavailableError,
    is_bluetooth_permission_error,
)


class WindowsBluetoothBackend(BleakCentralTransport):
    """Windows BLE client operations through Bleak and server through WinRT.

    WinRT objects and native callback event arguments remain in this backend.
    """

    def __init__(self) -> None:
        super().__init__(can_pair=True)
        self._server: WindowsGattPeripheral | None = None

    @property
    def capabilities(self) -> BluetoothCapabilities:
        return BluetoothCapabilities(
            discovery=True,
            advertising=True,
            hosting=True,
            pairing=True,
            multiple_peers=True,
            max_peers=4,
        )

    async def is_available(self) -> bool:
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter

            adapter = await BluetoothAdapter.get_default_async()
            return adapter is not None and bool(adapter.is_low_energy_supported)
        except Exception as exc:
            if is_bluetooth_permission_error(exc):
                raise BluetoothPermissionError(
                    "Windows denied access to the Bluetooth adapter. Check Windows privacy settings."
                ) from exc
            return False

    async def is_enabled(self) -> bool:
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter
            from winrt.windows.devices.radios import RadioState

            adapter = await BluetoothAdapter.get_default_async()
            if adapter is None:
                return False
            radio = await adapter.get_radio_async()
            return radio is not None and radio.state == RadioState.ON
        except Exception as exc:
            if is_bluetooth_permission_error(exc):
                raise BluetoothPermissionError(
                    "Windows denied access to Bluetooth radio state. Check Windows privacy settings."
                ) from exc
            return False

    async def start_server(self) -> None:
        if self._server is not None:
            raise BluetoothUnavailableError("BlueChat GATT server is already running")
        server = WindowsGattPeripheral()
        await server.start()
        self._server = server

    async def wait_host_failure(self) -> None:
        if self._server is None:
            raise BluetoothUnavailableError("BlueChat GATT server is not running")
        await self._server.wait_host_failure()

    async def advertise(self, room_metadata: dict[str, Any]) -> None:
        del room_metadata
        if self._server is None:
            raise BluetoothUnavailableError("Start the BlueChat GATT server before advertising")
        await self._server.advertise()

    async def accept(self) -> Connection:
        if self._server is None:
            raise BluetoothUnavailableError("BlueChat GATT server is not running")
        return await self._server.accept()

    async def stop_advertising(self) -> None:
        if self._server is not None:
            await self._server.stop_advertising()

    async def stop_server(self) -> None:
        if self._server is not None:
            await self._server.stop()
            self._server = None

    async def disconnect(self, peer_id: str) -> None:
        if self._server is not None:
            await self._server.disconnect(peer_id)
        await super().disconnect(peer_id)
