"""Cross-platform BLE GATT central/client implementation backed by Bleak."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from bluechat.bluetooth.base import (
    BluetoothCapabilities,
    BluetoothDevice,
    BluetoothTransport,
    Connection,
    PairResult,
)
from bluechat.bluetooth.gatt import (
    BLUECHAT_RX_UUID,
    BLUECHAT_SERVICE_UUID,
    BLUECHAT_TX_UUID,
    FRAGMENT_HEADER,
    FragmentReassembler,
    fragment_packet,
)
from bluechat.errors import (
    BluetoothCapabilityError,
    BluetoothPermissionError,
    BluetoothUnavailableError,
    ProtocolError,
    is_bluetooth_permission_error,
)

logger = logging.getLogger(__name__)


class BleakGattConnection:
    """Message-oriented connection over a BlueChat GATT service."""

    def __init__(self, client: Any, peer_id: str, *, max_write_size: int = 20) -> None:
        self.peer_id = peer_id
        self._client = client
        self._loop = asyncio.get_running_loop()
        self._fragments: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=4096)
        self._reassembler = FragmentReassembler()
        self._send_lock = asyncio.Lock()
        self._max_fragment_data = max(1, min(10, max_write_size - FRAGMENT_HEADER.size))
        self._closed = False

    def notification_callback(self, _characteristic: object, data: bytearray) -> None:
        try:
            self._loop.call_soon_threadsafe(self._enqueue_fragment, bytes(data))
        except RuntimeError:
            # Event loop shutdown races are normal during disconnect.
            pass

    def disconnected_callback(self, _client: object) -> None:
        try:
            self._loop.call_soon_threadsafe(self._enqueue_disconnect)
        except RuntimeError:
            pass

    def _enqueue_fragment(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            self._fragments.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("Dropping BLE notification after receive queue filled")

    def _enqueue_disconnect(self) -> None:
        self._closed = True
        if self._fragments.full():
            try:
                self._fragments.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._fragments.put_nowait(None)
        except asyncio.QueueFull:
            logger.warning("BLE receive queue full while signaling disconnect")

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("Bluetooth peer is disconnected")
        max_size = int(getattr(self._client, "mtu_size", 23)) - 3
        payload_size = max(1, min(self._max_fragment_data, max_size - FRAGMENT_HEADER.size))
        fragments = fragment_packet(data, fragment_size=payload_size)
        async with self._send_lock:
            try:
                for part in fragments:
                    await self._client.write_gatt_char(BLUECHAT_RX_UUID, part, response=True)
            except Exception as exc:
                raise BluetoothUnavailableError(
                    f"Bluetooth write failed ({type(exc).__name__})"
                ) from exc

    async def receive(self) -> bytes:
        while True:
            fragment = await self._fragments.get()
            if fragment is None:
                raise ConnectionError("Bluetooth peer disconnected")
            try:
                complete = self._reassembler.feed(fragment)
            except ProtocolError:
                # A malformed GATT packet is a peer protocol failure.
                await self.close()
                raise
            if complete is not None:
                return complete

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if getattr(self._client, "is_connected", False):
                await self._client.stop_notify(BLUECHAT_TX_UUID)
                await self._client.disconnect()
        finally:
            self._enqueue_disconnect()


class BleakCentralTransport(BluetoothTransport):
    """BLE scanning, pairing, and GATT client operations shared across desktop OSes."""

    def __init__(self, *, can_pair: bool = True) -> None:
        self._can_pair = can_pair
        self._devices: dict[str, object] = {}
        self._connections: dict[str, BleakGattConnection] = {}

    @property
    def capabilities(self) -> BluetoothCapabilities:
        return BluetoothCapabilities(discovery=True, pairing=self._can_pair)

    async def is_available(self) -> bool:
        try:
            from bleak import BleakScanner

            async with BleakScanner():
                pass
            return True
        except Exception as exc:
            logger.debug("BLE adapter availability check failed (%s)", type(exc).__name__)
            if is_bluetooth_permission_error(exc):
                raise BluetoothPermissionError(
                    "The operating system denied Bluetooth access. Grant Bluetooth permission to BlueChat and retry."
                ) from exc
            return False

    async def is_enabled(self) -> bool:
        # Bleak's cross-platform public API has no adapter power query. Starting a
        # scan distinguishes common disabled/unavailable errors without shelling out.
        return await self.is_available()

    async def discover(self, timeout: float = 8.0) -> list[BluetoothDevice]:
        if not 0.1 <= timeout <= 120.0:
            raise ValueError("scan timeout must be between 0.1 and 120 seconds")
        try:
            from bleak import BleakScanner

            found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        except Exception as exc:
            if is_bluetooth_permission_error(exc):
                raise BluetoothPermissionError(
                    "Bluetooth scan permission was denied by the operating system."
                ) from exc
            raise BluetoothUnavailableError(
                f"Bluetooth scan failed ({type(exc).__name__})"
            ) from exc
        devices: list[BluetoothDevice] = []
        self._devices.clear()
        for native, advertisement in found.values():
            identifier = str(native.address)
            self._devices[identifier] = native
            service_uuids = {value.lower() for value in (advertisement.service_uuids or [])}
            devices.append(
                BluetoothDevice(
                    name=native.name or advertisement.local_name or "Unknown device",
                    identifier=identifier,
                    address=identifier,
                    bluechat_host=BLUECHAT_SERVICE_UUID in service_uuids,
                )
            )
        return devices

    async def connect(self, device: BluetoothDevice) -> Connection:
        try:
            from bleak import BleakClient, BleakScanner

            native = self._devices.get(device.identifier)
            if native is None:
                native = await BleakScanner.find_device_by_address(device.identifier, timeout=8.0)
            if native is None:
                raise BluetoothUnavailableError("The selected device is no longer nearby")
            holder: dict[str, BleakGattConnection] = {}

            def on_disconnected(raw_client: Any) -> None:
                connection = holder.get("connection")
                if connection is not None:
                    connection.disconnected_callback(raw_client)

            client = BleakClient(cast(Any, native), disconnected_callback=on_disconnected)
            await client.connect()
            service = client.services.get_service(BLUECHAT_SERVICE_UUID)
            if service is None:
                await client.disconnect()
                raise BluetoothCapabilityError("The selected device is not hosting a BlueChat room")
            tx = service.get_characteristic(BLUECHAT_TX_UUID)
            rx = service.get_characteristic(BLUECHAT_RX_UUID)
            if tx is None or rx is None:
                await client.disconnect()
                raise BluetoothCapabilityError("The BlueChat GATT service is incomplete")
            connection = BleakGattConnection(
                client, device.identifier, max_write_size=rx.max_write_without_response_size
            )
            holder["connection"] = connection
            await client.start_notify(tx, connection.notification_callback)
            self._connections[device.identifier] = connection
            return connection
        except BluetoothUnavailableError:
            raise
        except Exception as exc:
            if is_bluetooth_permission_error(exc):
                raise BluetoothPermissionError(
                    "The operating system denied this Bluetooth connection. Check Bluetooth privacy settings."
                ) from exc
            raise BluetoothUnavailableError(
                f"Could not connect to Bluetooth peer ({type(exc).__name__})"
            ) from exc

    async def pair(self, device: BluetoothDevice) -> PairResult:
        if not self._can_pair:
            return PairResult.USER_ACTION_REQUIRED
        try:
            from bleak import BleakClient, BleakScanner

            native = self._devices.get(device.identifier)
            if native is None:
                native = await BleakScanner.find_device_by_address(device.identifier, timeout=8.0)
            if native is None:
                raise BluetoothUnavailableError("The selected device is no longer nearby")
            async with BleakClient(cast(Any, native), pair=True, timeout=30.0):
                pass
            return PairResult.PAIRED
        except BluetoothUnavailableError:
            raise
        except Exception as exc:
            if is_bluetooth_permission_error(exc):
                raise BluetoothPermissionError(
                    "The operating system denied Bluetooth pairing; confirm the system pairing prompt."
                ) from exc
            raise BluetoothUnavailableError(
                f"Pairing did not complete ({type(exc).__name__})"
            ) from exc

    async def advertise(self, room_metadata: dict[str, object]) -> None:
        del room_metadata
        raise BluetoothCapabilityError(
            "This OS backend supports BlueChat client connections, not hosting"
        )

    async def stop_advertising(self) -> None:
        return None

    async def start_server(self) -> None:
        raise BluetoothCapabilityError(
            "This OS backend supports BlueChat client connections, not hosting"
        )

    async def accept(self) -> Connection:
        raise BluetoothCapabilityError("This OS backend does not host rooms")

    async def stop_server(self) -> None:
        return None

    async def disconnect(self, peer_id: str) -> None:
        connection = self._connections.pop(peer_id, None)
        if connection is not None:
            await connection.close()
