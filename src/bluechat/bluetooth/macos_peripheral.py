"""CoreBluetooth GATT peripheral implementation for macOS hosting.

CoreBluetooth delegate callbacks are delivered on a private dispatch queue and
bridged back to the asyncio loop. This module is imported only by the macOS
backend; no Objective-C objects cross the BluetoothTransport boundary.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from bluechat.bluetooth.base import Connection
from bluechat.bluetooth.gatt import (
    BLUECHAT_RX_UUID,
    BLUECHAT_SERVICE_UUID,
    BLUECHAT_TX_UUID,
    FragmentReassembler,
    fragment_packet,
)
from bluechat.errors import BluetoothUnavailableError, ProtocolError

logger = logging.getLogger(__name__)
MAX_PEERS = 4
QUEUE_SIZE = 128


class CoreBluetoothPeripheral:
    """Async lifecycle and connections for a native CoreBluetooth peripheral."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._states: asyncio.Queue[int] = asyncio.Queue(maxsize=8)
        self._service_ready: asyncio.Future[None] | None = None
        self._advertised: asyncio.Future[None] | None = None
        self._ready = asyncio.Event()
        self._host_failure = asyncio.Event()
        self._powered_on = False
        self._powered_on_value: int | None = None
        self._peripheral: Any = None
        self._service: Any = None
        self._rx: Any = None
        self._tx: Any = None
        self._delegate: Any = None
        self._dispatch_queue: Any = None
        self._dispatch_async: Any = None
        self._peers: dict[str, CoreBluetoothConnection] = {}
        self._centrals: dict[str, Any] = {}
        self._accepted: asyncio.Queue[CoreBluetoothConnection] = asyncio.Queue(MAX_PEERS)
        self._send_lock = asyncio.Lock()
        self._stopped = False

    async def start(self) -> None:
        try:
            from CoreBluetooth import (
                CBAdvertisementDataServiceUUIDsKey,
                CBManagerStatePoweredOff,
                CBManagerStatePoweredOn,
                CBManagerStateUnauthorized,
                CBManagerStateUnsupported,
                CBMutableCharacteristic,
                CBMutableService,
                CBCharacteristicPropertyIndicate,
                CBCharacteristicPropertyWrite,
                CBAttributePermissionsReadable,
                CBAttributePermissionsWriteable,
                CBPeripheralManager,
                CBUUID,
            )
            from Foundation import NSObject
            from libdispatch import dispatch_async, dispatch_queue_create

            self._powered_on_value = int(CBManagerStatePoweredOn)
            owner = self

            class Delegate(NSObject):
                def peripheralManagerDidUpdateState_(self, manager: Any) -> None:
                    owner._call(owner._on_state, int(manager.state()))

                def peripheralManager_didAddService_error_(
                    self, manager: Any, service: Any, error: Any
                ) -> None:
                    owner._call(owner._on_service_added, error)

                def peripheralManagerDidStartAdvertising_error_(
                    self, manager: Any, error: Any
                ) -> None:
                    owner._call(owner._on_advertising, error)

                def peripheralManager_central_didSubscribeToCharacteristic_(
                    self, manager: Any, central: Any, characteristic: Any
                ) -> None:
                    owner._call(owner._on_subscribe, central)

                def peripheralManager_central_didUnsubscribeFromCharacteristic_(
                    self, manager: Any, central: Any, characteristic: Any
                ) -> None:
                    owner._call(owner._on_unsubscribe, central)

                def peripheralManager_didReceiveWriteRequests_(
                    self, manager: Any, requests: Any
                ) -> None:
                    owner._call(owner._on_write, manager, list(requests))

                def peripheralManagerIsReadyToUpdateSubscribers_(self, manager: Any) -> None:
                    owner._call(owner._ready.set)

            self._delegate = Delegate.alloc().init()
            queue = dispatch_queue_create(b"org.bluechat.corebluetooth.peripheral", None)
            self._dispatch_queue = queue
            self._dispatch_async = dispatch_async
            self._peripheral = CBPeripheralManager.alloc().initWithDelegate_queue_options_(
                self._delegate, queue, None
            )
            while True:
                state = await asyncio.wait_for(self._states.get(), timeout=15)
                if state == CBManagerStatePoweredOn:
                    break
                if state in (
                    CBManagerStatePoweredOff,
                    CBManagerStateUnauthorized,
                    CBManagerStateUnsupported,
                ):
                    guidance = (
                        "Enable Bluetooth in System Settings and allow BlueChat to access it."
                        if state != CBManagerStateUnsupported
                        else "This Mac does not expose Bluetooth LE peripheral support."
                    )
                    raise BluetoothUnavailableError(
                        f"CoreBluetooth cannot host BlueChat. {guidance}"
                    )

            service_uuid = CBUUID.UUIDWithString_(BLUECHAT_SERVICE_UUID)
            rx_uuid = CBUUID.UUIDWithString_(BLUECHAT_RX_UUID)
            tx_uuid = CBUUID.UUIDWithString_(BLUECHAT_TX_UUID)
            self._rx = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
                rx_uuid,
                CBCharacteristicPropertyWrite,
                None,
                CBAttributePermissionsWriteable,
            )
            self._tx = CBMutableCharacteristic.alloc().initWithType_properties_value_permissions_(
                tx_uuid,
                CBCharacteristicPropertyIndicate,
                None,
                CBAttributePermissionsReadable,
            )
            self._service = CBMutableService.alloc().initWithType_primary_(service_uuid, True)
            self._service.setCharacteristics_([self._rx, self._tx])
            self._service_ready = self._loop.create_future()
            # Keep the key and UUID alive for frameworks that retain the dict
            # asynchronously until advertising has started.
            self._advertisement = {
                CBAdvertisementDataServiceUUIDsKey: [service_uuid],
            }
            await self._invoke(self._peripheral.addService_, self._service)
            await asyncio.wait_for(asyncio.shield(self._service_ready), timeout=15)
        except BluetoothUnavailableError:
            await self.stop()
            raise
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise BluetoothUnavailableError(
                "Timed out waiting for CoreBluetooth; check Bluetooth permission and system settings"
            ) from exc
        except Exception as exc:
            await self.stop()
            raise BluetoothUnavailableError(
                f"Could not initialize CoreBluetooth peripheral ({type(exc).__name__})"
            ) from exc

    async def advertise(self) -> None:
        if self._peripheral is None or self._service is None:
            raise BluetoothUnavailableError(
                "Start the CoreBluetooth GATT server before advertising"
            )
        if self._advertised is not None and not self._advertised.done():
            raise BluetoothUnavailableError("BlueChat is already being advertised")
        self._advertised = self._loop.create_future()
        try:
            await self._invoke(self._peripheral.startAdvertising_, self._advertisement)
            await asyncio.wait_for(asyncio.shield(self._advertised), timeout=15)
        except asyncio.TimeoutError as exc:
            raise BluetoothUnavailableError(
                "Timed out while starting the BlueChat advertisement"
            ) from exc

    async def accept(self) -> Connection:
        return await self._accepted.get()

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        peripheral = self._peripheral
        if peripheral is not None:
            try:
                if self._dispatch_queue is not None:
                    await self._invoke(peripheral.stopAdvertising)
                    await self._invoke(peripheral.removeAllServices)
            except Exception as exc:
                logger.debug("CoreBluetooth cleanup failed (%s)", type(exc).__name__)
        for peer in tuple(self._peers.values()):
            peer.mark_disconnected()
        self._peers.clear()
        self._centrals.clear()
        while not self._accepted.empty():
            try:
                self._accepted.get_nowait().mark_disconnected()
            except asyncio.QueueEmpty:
                break
        self._peripheral = None
        self._service = None
        self._rx = None
        self._tx = None
        self._delegate = None
        self._dispatch_queue = None
        self._dispatch_async = None

    async def stop_advertising(self) -> None:
        if self._peripheral is None:
            return
        try:
            await self._invoke(self._peripheral.stopAdvertising)
        except Exception as exc:
            logger.debug("CoreBluetooth advertisement stop failed (%s)", type(exc).__name__)
        self._advertised = None

    async def wait_host_failure(self) -> None:
        await self._host_failure.wait()

    async def disconnect(self, peer_id: str) -> None:
        peer = self._peers.pop(peer_id, None)
        self._centrals.pop(peer_id, None)
        if peer is not None:
            peer.mark_disconnected()
        # CoreBluetooth intentionally has no API to disconnect a specific
        # central; closing our logical channel is the supported action.

    async def _on_service_added(self, error: Any) -> None:
        future = self._service_ready
        if future is None or future.done():
            return
        if error is not None:
            future.set_exception(
                BluetoothUnavailableError("CoreBluetooth could not publish the GATT service")
            )
            return
        future.set_result(None)

    async def _on_advertising(self, error: Any) -> None:
        self._finish_advertisement(
            BluetoothUnavailableError("CoreBluetooth failed to start advertising")
            if error is not None
            else None
        )

    def _finish_advertisement(self, error: Exception | None) -> None:
        future = self._advertised
        if future is None or future.done():
            return
        if error is None:
            future.set_result(None)
        else:
            future.set_exception(error)

    async def _on_state(self, state: int) -> None:
        if self._powered_on and state != self._powered_on_value:
            self._host_failure.set()
        if state == self._powered_on_value:
            self._powered_on = True
        try:
            self._states.put_nowait(state)
        except asyncio.QueueFull:
            # Keep the most recent state if callbacks arrive faster than startup.
            try:
                self._states.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._states.put_nowait(state)

    async def _on_subscribe(self, central: Any) -> None:
        peer_id = str(central.identifier().UUIDString())
        self._centrals[peer_id] = central
        peer = self._peers.get(peer_id)
        if peer is None:
            if len(self._peers) >= MAX_PEERS:
                logger.info(
                    "CoreBluetooth central rejected because BlueChat room transport is full"
                )
                return
            peer = CoreBluetoothConnection(peer_id, self)
            self._peers[peer_id] = peer
            try:
                self._accepted.put_nowait(peer)
            except asyncio.QueueFull:
                self._peers.pop(peer_id, None)

    async def _on_unsubscribe(self, central: Any) -> None:
        peer_id = str(central.identifier().UUIDString())
        peer = self._peers.pop(peer_id, None)
        self._centrals.pop(peer_id, None)
        if peer is not None:
            peer.mark_disconnected()

    async def _on_write(self, manager: Any, requests: list[Any]) -> None:
        # A GATT write batch is acknowledged as a batch by CoreBluetooth.
        for request in requests:
            try:
                if request.characteristic().UUID().UUIDString().lower() != BLUECHAT_RX_UUID:
                    from CoreBluetooth import CBATTErrorAttributeNotFound

                    await self._invoke(
                        manager.respondToRequest_withResult_, request, CBATTErrorAttributeNotFound
                    )
                    continue
                central = request.central()
                peer_id = str(central.identifier().UUIDString())
                peer = self._peers.get(peer_id)
                if peer is None:
                    peer = CoreBluetoothConnection(peer_id, self)
                    self._peers[peer_id] = peer
                    self._centrals[peer_id] = central
                    try:
                        self._accepted.put_nowait(peer)
                    except asyncio.QueueFull:
                        self._peers.pop(peer_id, None)
                        from CoreBluetooth import CBATTErrorInsufficientResources

                        await self._invoke(
                            manager.respondToRequest_withResult_,
                            request,
                            CBATTErrorInsufficientResources,
                        )
                        continue
                value = bytes(request.value()) if request.value() is not None else b""
                peer.feed_fragment(value)
                from CoreBluetooth import CBATTErrorSuccess

                await self._invoke(manager.respondToRequest_withResult_, request, CBATTErrorSuccess)
            except Exception as exc:
                logger.debug("Rejected CoreBluetooth write (%s)", type(exc).__name__)

    def _call(self, callback: Any, *args: Any) -> None:
        if self._stopped or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._schedule_callback, callback, args)

    async def _invoke(self, callback: Any, *args: Any) -> Any:
        """Run a CoreBluetooth operation on its delegate queue."""
        if self._dispatch_queue is None or self._dispatch_async is None:
            raise BluetoothUnavailableError("CoreBluetooth dispatch queue is not running")
        future: asyncio.Future[Any] = self._loop.create_future()

        def complete(value: Any = None, error: BaseException | None = None) -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value)

        def run_native() -> None:
            try:
                value = callback(*args)
            except BaseException as exc:
                self._loop.call_soon_threadsafe(complete, None, exc)
            else:
                self._loop.call_soon_threadsafe(complete, value, None)

        self._dispatch_async(self._dispatch_queue, run_native)
        return await future

    async def update_value(self, fragment: bytes, peer_id: str) -> bool:
        central = self._centrals.get(peer_id)
        if central is None or self._peripheral is None or self._tx is None:
            raise ConnectionError("Bluetooth peer disconnected")
        from Foundation import NSData

        value = NSData.dataWithBytes_length_(fragment, len(fragment))
        return bool(
            await self._invoke(
                self._peripheral.updateValue_forCharacteristic_onSubscribedCentrals_,
                value,
                self._tx,
                [central],
            )
        )

    @staticmethod
    def _schedule_callback(callback: Any, args: tuple[Any, ...]) -> None:
        task = asyncio.create_task(callback(*args))
        task.add_done_callback(CoreBluetoothPeripheral._log_callback_error)

    @staticmethod
    def _log_callback_error(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except Exception as exc:
            logger.debug("CoreBluetooth delegate callback failed (%s)", type(exc).__name__)


class CoreBluetoothConnection:
    """Message-oriented peer pipe using GATT writes and indications."""

    def __init__(self, peer_id: str, server: CoreBluetoothPeripheral) -> None:
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
            raise ProtocolError("CoreBluetooth receive queue is full") from exc

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("Bluetooth peer disconnected")
        fragments = fragment_packet(data)
        async with self._server._send_lock:
            for fragment in fragments:
                while True:
                    self._server._ready.clear()
                    sent = await self._server.update_value(fragment, self.peer_id)
                    if sent:
                        break
                    try:
                        await asyncio.wait_for(self._server._ready.wait(), timeout=10)
                    except asyncio.TimeoutError as exc:
                        raise BluetoothUnavailableError(
                            "CoreBluetooth notification queue remained backpressured"
                        ) from exc

    async def receive(self) -> bytes:
        packet = await self._incoming.get()
        if packet is None:
            raise ConnectionError("Bluetooth peer disconnected")
        return packet

    async def close(self) -> None:
        self.mark_disconnected()
        self._server._peers.pop(self.peer_id, None)
        self._server._centrals.pop(self.peer_id, None)

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
