"""Configurable async backend for deterministic, hardware-free tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from bluechat.bluetooth.base import (
    BluetoothCapabilities,
    BluetoothDevice,
    BluetoothTransport,
    Connection,
    PairResult,
)
from bluechat.errors import BluetoothUnavailableError


class FakeBluetoothTransport(BluetoothTransport):
    """Inject discovered devices and accepted connections without OS Bluetooth."""

    def __init__(
        self,
        devices: list[BluetoothDevice] | None = None,
        *,
        available: bool = True,
        enabled: bool = True,
        connect_factory: Callable[[BluetoothDevice], Connection] | None = None,
    ) -> None:
        self.devices = list(devices or [])
        self.available = available
        self.enabled = enabled
        self.connect_factory = connect_factory
        self._incoming: asyncio.Queue[Connection] = asyncio.Queue()
        self._advertising = False
        self._serving = False
        self.room_metadata: dict[str, object] = {}
        self._connections: dict[str, Connection] = {}

    @property
    def capabilities(self) -> BluetoothCapabilities:
        return BluetoothCapabilities(True, True, True, True, True, max_peers=4)

    async def is_available(self) -> bool:
        return self.available

    async def is_enabled(self) -> bool:
        return self.enabled

    async def discover(self, timeout: float = 8.0) -> list[BluetoothDevice]:
        if not 0.1 <= timeout <= 120:
            raise ValueError("scan timeout must be between 0.1 and 120 seconds")
        self._check_ready()
        return list(self.devices)

    async def advertise(self, room_metadata: dict[str, object]) -> None:
        self._check_ready()
        self.room_metadata = dict(room_metadata)
        self._advertising = True

    async def stop_advertising(self) -> None:
        self._advertising = False

    async def connect(self, device: BluetoothDevice) -> Connection:
        self._check_ready()
        if self.connect_factory is None:
            raise BluetoothUnavailableError("No fake connection factory was configured")
        connection = self.connect_factory(device)
        self._connections[connection.peer_id] = connection
        return connection

    async def start_server(self) -> None:
        self._check_ready()
        self._serving = True

    async def accept(self) -> Connection:
        if not self._serving:
            raise BluetoothUnavailableError("Fake room server is not running")
        connection = await self._incoming.get()
        self._connections[connection.peer_id] = connection
        return connection

    async def enqueue_connection(self, connection: Connection) -> None:
        """Test helper used to emulate a just-connected remote participant."""
        if not self._serving:
            raise BluetoothUnavailableError("Fake room server is not running")
        await self._incoming.put(connection)

    async def stop_server(self) -> None:
        self._serving = False
        for connection in tuple(self._connections.values()):
            await connection.close()
        self._connections.clear()

    async def pair(self, device: BluetoothDevice) -> PairResult:
        self._check_ready()
        for index, candidate in enumerate(self.devices):
            if candidate.identifier == device.identifier:
                self.devices[index] = BluetoothDevice(
                    name=candidate.name,
                    identifier=candidate.identifier,
                    address=candidate.address,
                    paired=True,
                    bluechat_host=candidate.bluechat_host,
                    details=candidate.details,
                )
                return PairResult.PAIRED
        return PairResult.USER_ACTION_REQUIRED

    async def disconnect(self, peer_id: str) -> None:
        connection = self._connections.pop(peer_id, None)
        if connection is not None:
            await connection.close()

    def _check_ready(self) -> None:
        if not self.available:
            raise BluetoothUnavailableError("Fake Bluetooth adapter is unavailable")
        if not self.enabled:
            raise BluetoothUnavailableError("Fake Bluetooth is turned off")


# Keep the old exported name for the in-progress foundation API.
FakeBluetoothBackend = FakeBluetoothTransport
