"""Windows GATT peripheral/server using the maintained PyWinRT projections."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from bluechat.bluetooth.base import Connection
from bluechat.bluetooth.gatt import (
    BLUECHAT_RX_UUID,
    BLUECHAT_SERVICE_UUID,
    BLUECHAT_TX_UUID,
    FragmentReassembler,
    fragment_packet,
)
from bluechat.errors import BluetoothCapabilityError, BluetoothUnavailableError, ProtocolError

logger = logging.getLogger(__name__)
MAX_PEERS = 4
QUEUE_SIZE = 128


class WindowsGattPeripheral:
    """Windows Runtime GATT service provider bridged to asyncio connections."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._provider: Any = None
        self._rx: Any = None
        self._tx: Any = None
        self._advertisement_started = asyncio.Event()
        self._host_failure = asyncio.Event()
        self._subscribers: dict[str, Any] = {}
        self._peers: dict[str, WindowsGattConnection] = {}
        self._accepted: asyncio.Queue[WindowsGattConnection] = asyncio.Queue(MAX_PEERS)
        self._send_lock = asyncio.Lock()
        self._stopped = False

    async def start(self) -> None:
        try:
            from winrt.windows.devices.bluetooth import BluetoothAdapter
            from winrt.windows.devices.bluetooth.genericattributeprofile import (
                GattCharacteristicProperties,
                GattLocalCharacteristicParameters,
                GattProtectionLevel,
                GattServiceProvider,
            )

            adapter = await BluetoothAdapter.get_default_async()
            if adapter is None or not adapter.is_low_energy_supported:
                raise BluetoothUnavailableError(
                    "Windows did not report a BLE-capable Bluetooth adapter"
                )
            if not adapter.is_peripheral_role_supported:
                raise BluetoothCapabilityError(
                    "This Windows adapter or driver cannot advertise BLE GATT services as a host"
                )

            result = await GattServiceProvider.create_async(UUID(BLUECHAT_SERVICE_UUID))
            if str(result.error).split(".")[-1].casefold() != "success":
                raise BluetoothUnavailableError(
                    f"Windows could not create the BlueChat GATT service ({result.error})"
                )
            self._provider = result.service_provider
            self._host_failure.clear()
            self._provider.add_advertisement_status_changed(self._advertisement_status_changed)

            rx_parameters = GattLocalCharacteristicParameters()
            rx_parameters.characteristic_properties = GattCharacteristicProperties.WRITE
            rx_parameters.write_protection_level = GattProtectionLevel.PLAIN
            rx_result = await self._provider.service.create_characteristic_async(
                UUID(BLUECHAT_RX_UUID), rx_parameters
            )
            if str(rx_result.error).split(".")[-1].casefold() != "success":
                raise BluetoothUnavailableError(
                    "Windows could not create the BlueChat RX characteristic"
                )
            self._rx = rx_result.characteristic
            self._rx.add_write_requested(self._write_requested)

            tx_parameters = GattLocalCharacteristicParameters()
            tx_parameters.characteristic_properties = GattCharacteristicProperties.INDICATE
            tx_parameters.write_protection_level = GattProtectionLevel.PLAIN
            tx_result = await self._provider.service.create_characteristic_async(
                UUID(BLUECHAT_TX_UUID), tx_parameters
            )
            if str(tx_result.error).split(".")[-1].casefold() != "success":
                raise BluetoothUnavailableError(
                    "Windows could not create the BlueChat TX characteristic"
                )
            self._tx = tx_result.characteristic
            self._tx.add_subscribed_clients_changed(self._subscribed_clients_changed)
        except (BluetoothUnavailableError, BluetoothCapabilityError):
            await self.stop()
            raise
        except Exception as exc:
            await self.stop()
            raise BluetoothUnavailableError(
                f"Could not initialize the Windows GATT server ({type(exc).__name__}); check Bluetooth permissions"
            ) from exc

    async def advertise(self) -> None:
        if self._provider is None:
            raise BluetoothUnavailableError(
                "Start the Windows BlueChat GATT service before advertising"
            )
        try:
            from winrt.windows.devices.bluetooth.genericattributeprofile import (
                GattServiceProviderAdvertisingParameters,
            )

            parameters = GattServiceProviderAdvertisingParameters()
            parameters.is_discoverable = True
            parameters.is_connectable = True
            self._advertisement_started.clear()
            self._provider.start_advertising_with_parameters(parameters)
            await asyncio.wait_for(self._advertisement_started.wait(), timeout=15)
            status = str(self._provider.advertisement_status).split(".")[-1].casefold()
            if status != "started":
                raise BluetoothUnavailableError(
                    f"Windows BlueChat advertisement did not start ({status})"
                )
        except asyncio.TimeoutError as exc:
            raise BluetoothUnavailableError(
                "Windows timed out starting the BlueChat advertisement"
            ) from exc
        except BluetoothUnavailableError:
            raise
        except Exception as exc:
            raise BluetoothUnavailableError(
                f"Windows could not advertise the BlueChat service ({type(exc).__name__})"
            ) from exc

    async def accept(self) -> Connection:
        return await self._accepted.get()

    async def stop_advertising(self) -> None:
        provider = self._provider
        if provider is not None:
            try:
                provider.stop_advertising()
            except Exception as exc:
                logger.debug("Windows advertisement cleanup failed (%s)", type(exc).__name__)

    async def wait_host_failure(self) -> None:
        await self._host_failure.wait()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        await self.stop_advertising()
        for peer in tuple(self._peers.values()):
            peer.mark_disconnected()
        self._peers.clear()
        self._subscribers.clear()
        while not self._accepted.empty():
            try:
                self._accepted.get_nowait().mark_disconnected()
            except asyncio.QueueEmpty:
                break
        self._provider = None
        self._rx = None
        self._tx = None

    async def disconnect(self, peer_id: str) -> None:
        peer = self._peers.pop(peer_id, None)
        self._subscribers.pop(peer_id, None)
        if peer is not None:
            peer.mark_disconnected()

    def _advertisement_status_changed(self, sender: Any, _args: Any) -> None:
        status = str(sender.advertisement_status).split(".")[-1].casefold()
        if status == "aborted":
            self._loop.call_soon_threadsafe(self._host_failure.set)
        if status in {"started", "aborted", "stopped"}:
            self._loop.call_soon_threadsafe(self._advertisement_started.set)

    def _subscribed_clients_changed(self, characteristic: Any, _args: Any) -> None:
        try:
            clients = list(characteristic.subscribed_clients)
        except Exception as exc:
            logger.debug("Could not query Windows GATT subscribers (%s)", type(exc).__name__)
            return
        self._loop.call_soon_threadsafe(self._apply_subscribers, clients)

    def _apply_subscribers(self, clients: list[Any]) -> None:
        current: dict[str, Any] = {}
        for client in clients:
            try:
                peer_id = str(client.session.device_id.id)
            except Exception:
                continue
            current[peer_id] = client
            if peer_id not in self._peers and len(self._peers) < MAX_PEERS:
                peer = WindowsGattConnection(peer_id, self)
                self._peers[peer_id] = peer
                try:
                    self._accepted.put_nowait(peer)
                except asyncio.QueueFull:
                    self._peers.pop(peer_id, None)
        for peer_id in set(self._subscribers).difference(current):
            disconnected_peer = self._peers.get(peer_id)
            if disconnected_peer is not None:
                del self._peers[peer_id]
                disconnected_peer.mark_disconnected()
        self._subscribers = current

    def _write_requested(self, _characteristic: Any, args: Any) -> None:
        try:
            deferral = args.get_deferral()
            self._loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._process_write(args, deferral))
            )
        except Exception as exc:
            logger.debug("Could not schedule Windows GATT write (%s)", type(exc).__name__)

    async def _process_write(self, args: Any, deferral: Any) -> None:
        request: Any = None
        try:
            request = await args.get_request_async()
            if request is None:
                return
            peer_id = str(args.session.device_id.id)
            peer = self._peers.get(peer_id)
            if peer is None:
                if len(self._peers) >= MAX_PEERS:
                    request.respond_with_protocol_error(0x11)  # Insufficient Resources.
                    return
                peer = WindowsGattConnection(peer_id, self)
                self._peers[peer_id] = peer
                try:
                    self._accepted.put_nowait(peer)
                except asyncio.QueueFull:
                    self._peers.pop(peer_id, None)
                    request.respond_with_protocol_error(0x11)
                    return
            if request.offset != 0:
                request.respond_with_protocol_error(0x07)  # Invalid Offset.
                return
            from winrt.windows.storage.streams import DataReader

            reader = DataReader.from_buffer(request.value)
            value_buffer = bytearray(reader.unconsumed_buffer_length)
            reader.read_bytes(value_buffer)
            value = bytes(value_buffer)
            peer.feed_fragment(value)
            request.respond()
        except (ProtocolError, BufferError, ValueError) as exc:
            logger.info("Rejected invalid Windows GATT write (%s)", type(exc).__name__)
            if request is not None:
                try:
                    request.respond_with_protocol_error(0x0E)  # Unlikely Error.
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Windows GATT write processing failed (%s)", type(exc).__name__)
            if request is not None:
                try:
                    request.respond_with_protocol_error(0x0E)
                except Exception:
                    pass
        finally:
            try:
                deferral.complete()
            except Exception:
                pass


class WindowsGattConnection:
    """One connected Windows GATT central as a BlueChat packet stream."""

    def __init__(self, peer_id: str, server: WindowsGattPeripheral) -> None:
        self.peer_id = peer_id
        self._server = server
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue(QUEUE_SIZE)
        self._reassembler = FragmentReassembler()
        self._closed = False

    def feed_fragment(self, fragment: bytes) -> None:
        if self._closed:
            return
        packet = self._reassembler.feed(fragment)
        if packet is None:
            return
        try:
            self._incoming.put_nowait(packet)
        except asyncio.QueueFull as exc:
            self.mark_disconnected()
            raise ProtocolError("Windows GATT receive queue is full") from exc

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("Bluetooth peer disconnected")
        from winrt.windows.devices.bluetooth.genericattributeprofile import GattCommunicationStatus
        from winrt.windows.storage.streams import DataWriter

        async with self._server._send_lock:
            for fragment in fragment_packet(data):
                client = self._server._subscribers.get(self.peer_id)
                if client is None:
                    self.mark_disconnected()
                    raise ConnectionError("Bluetooth peer disconnected")
                writer = DataWriter()
                writer.write_bytes(bytes(fragment))
                result = await self._server._tx.notify_value_for_subscribed_client_async(
                    writer.detach_buffer(), client
                )
                if result.status != GattCommunicationStatus.SUCCESS:
                    raise BluetoothUnavailableError(
                        f"Windows BLE indication failed ({result.status})"
                    )

    async def receive(self) -> bytes:
        packet = await self._incoming.get()
        if packet is None:
            raise ConnectionError("Bluetooth peer disconnected")
        return packet

    async def close(self) -> None:
        self.mark_disconnected()
        self._server._peers.pop(self.peer_id, None)
        self._server._subscribers.pop(self.peer_id, None)

    def mark_disconnected(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._incoming.full():
            try:
                self._incoming.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._incoming.put_nowait(None)
        except asyncio.QueueFull:
            pass
