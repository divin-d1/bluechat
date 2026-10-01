"""macOS BLE central/client and native CoreBluetooth peripheral/server."""

from typing import Any

from bluechat.bluetooth.base import BluetoothCapabilities, Connection
from bluechat.bluetooth.bleak_central import BleakCentralTransport
from bluechat.bluetooth.macos_peripheral import CoreBluetoothPeripheral
from bluechat.errors import BluetoothUnavailableError


class MacOSBluetoothBackend(BleakCentralTransport):
    """macOS BLE discovery/client via Bleak, hosting via CoreBluetooth.

    CoreBluetooth pairing is managed by the OS on access to protected
    characteristics. The peripheral manager stays isolated in the native
    backend and communicates with the shared protocol through ``Connection``.
    """

    def __init__(self) -> None:
        super().__init__(can_pair=False)
        self._server: CoreBluetoothPeripheral | None = None

    @property
    def capabilities(self) -> BluetoothCapabilities:
        return BluetoothCapabilities(
            discovery=True,
            advertising=True,
            hosting=True,
            pairing=False,
            multiple_peers=True,
            max_peers=4,
        )

    async def start_server(self) -> None:
        if self._server is not None:
            raise BluetoothUnavailableError("BlueChat GATT server is already running")
        server = CoreBluetoothPeripheral()
        await server.start()
        self._server = server

    async def wait_host_failure(self) -> None:
        if self._server is None:
            raise BluetoothUnavailableError("BlueChat GATT server is not running")
        await self._server.wait_host_failure()

    async def advertise(self, room_metadata: dict[str, Any]) -> None:
        del room_metadata  # CoreBluetooth advertisement data is intentionally minimal.
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
