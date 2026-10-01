"""Linux BLE central plus BlueZ D-Bus GATT peripheral/server."""

# dbus-next intentionally uses D-Bus signatures in annotations (for example
# ``"a{sv}"``), which are runtime metadata rather than Python types.
# mypy: disable-error-code="valid-type,name-defined"

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from bluechat.bluetooth.base import BluetoothCapabilities, BluetoothDevice, Connection
from bluechat.bluetooth.bleak_central import BleakCentralTransport
from bluechat.bluetooth.gatt import (
    BLUECHAT_RX_UUID,
    BLUECHAT_SERVICE_UUID,
    BLUECHAT_TX_UUID,
    FragmentReassembler,
    fragment_packet,
)
from bluechat.errors import (
    BluetoothPermissionError,
    BluetoothUnavailableError,
    ProtocolError,
    is_bluetooth_permission_error,
)

logger = logging.getLogger(__name__)

BLUEZ = "org.bluez"
ROOT = "/org/bluez"
APP_PATH = "/org/bluechat"
SERVICE_PATH = APP_PATH + "/service0"
RX_PATH = SERVICE_PATH + "/char0"
TX_PATH = SERVICE_PATH + "/char1"
ADV_PATH = "/org/bluechat/advertisement0"


class BlueZPeerConnection:
    """One central's logical packet stream; host TX notifications are broadcast."""

    def __init__(self, peer_id: str, tx: BlueChatTxCharacteristic) -> None:
        self.peer_id = peer_id
        self._tx = tx
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=128)
        self._reassembler = FragmentReassembler()
        self._closed = False

    def feed_fragment(self, value: bytes) -> None:
        if self._closed:
            return
        complete = self._reassembler.feed(value)
        if complete is not None:
            try:
                self._incoming.put_nowait(complete)
            except asyncio.QueueFull:
                logger.warning("Dropping a BlueChat packet from peer after queue overflow")

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise ConnectionError("Bluetooth peer disconnected")
        for fragment in fragment_packet(data):
            await self._tx.notify_value(fragment)

    async def receive(self) -> bytes:
        value = await self._incoming.get()
        if value is None:
            raise ConnectionError("Bluetooth peer disconnected")
        return value

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._incoming.put_nowait(None)
            except asyncio.QueueFull:
                pass

    def mark_disconnected(self) -> None:
        if not self._closed:
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


class BlueChatObjectManager:
    """BlueZ requires one ObjectManager owning the complete application tree."""

    def __init__(self) -> None:
        from dbus_next import Variant
        from dbus_next.service import ServiceInterface, method

        class Interface(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.freedesktop.DBus.ObjectManager")

            @method()
            def GetManagedObjects(self) -> "a{oa{sa{sv}}}":
                return {
                    SERVICE_PATH: {
                        "org.bluez.GattService1": {
                            "UUID": Variant("s", BLUECHAT_SERVICE_UUID),
                            "Primary": Variant("b", True),
                            "Includes": Variant("as", []),
                            "Characteristics": Variant("ao", [RX_PATH, TX_PATH]),
                        }
                    },
                    RX_PATH: {
                        "org.bluez.GattCharacteristic1": {
                            "Service": Variant("o", SERVICE_PATH),
                            "UUID": Variant("s", BLUECHAT_RX_UUID),
                            "Flags": Variant("as", ["write", "write-without-response"]),
                            "Descriptors": Variant("ao", []),
                        }
                    },
                    TX_PATH: {
                        "org.bluez.GattCharacteristic1": {
                            "Service": Variant("o", SERVICE_PATH),
                            "UUID": Variant("s", BLUECHAT_TX_UUID),
                            "Flags": Variant("as", ["notify"]),
                            "Descriptors": Variant("ao", []),
                        }
                    },
                }

        self.interface = Interface()


class BlueChatGattService:
    def __init__(self) -> None:
        from dbus_next.service import PropertyAccess, ServiceInterface, dbus_property

        class Interface(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.bluez.GattService1")

            @dbus_property(access=PropertyAccess.READ)
            def UUID(self) -> "s":
                return BLUECHAT_SERVICE_UUID

            @dbus_property(access=PropertyAccess.READ)
            def Primary(self) -> "b":
                return True

            @dbus_property(access=PropertyAccess.READ)
            def Includes(self) -> "as":
                return []

            @dbus_property(access=PropertyAccess.READ)
            def Characteristics(self) -> "ao":
                return [RX_PATH, TX_PATH]

        self.interface = Interface()


class BlueChatRxCharacteristic:
    def __init__(self, server: BlueZGattServer) -> None:
        from dbus_next.service import PropertyAccess, ServiceInterface, dbus_property, method

        class Interface(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.bluez.GattCharacteristic1")

            @dbus_property(access=PropertyAccess.READ)
            def Service(self) -> "o":
                return SERVICE_PATH

            @dbus_property(access=PropertyAccess.READ)
            def UUID(self) -> "s":
                return BLUECHAT_RX_UUID

            @dbus_property(access=PropertyAccess.READ)
            def Flags(self) -> "as":
                return ["write", "write-without-response"]

            @dbus_property(access=PropertyAccess.READ)
            def Descriptors(self) -> "ao":
                return []

            @method()
            def ReadValue(self, _options: "a{sv}") -> "ay":
                return b""

            @method()
            def WriteValue(self, value: "ay", options: "a{sv}"):
                device = options.get("device")
                peer_path = str(getattr(device, "value", device or "unknown"))
                server.receive_fragment(peer_path, bytes(value))

        self.interface = Interface()


class BlueChatTxCharacteristic:
    def __init__(self) -> None:
        from dbus_next.service import PropertyAccess, ServiceInterface, dbus_property, method

        class Interface(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.bluez.GattCharacteristic1")
                self.value = b""
                self.notifying = False

            @dbus_property(access=PropertyAccess.READ)
            def Service(self) -> "o":
                return SERVICE_PATH

            @dbus_property(access=PropertyAccess.READ)
            def UUID(self) -> "s":
                return BLUECHAT_TX_UUID

            @dbus_property(access=PropertyAccess.READ)
            def Flags(self) -> "as":
                return ["indicate"]

            @dbus_property(access=PropertyAccess.READ)
            def Descriptors(self) -> "ao":
                return []

            @dbus_property(access=PropertyAccess.READ)
            def Notifying(self) -> "b":
                return self.notifying

            @dbus_property(access=PropertyAccess.READ)
            def Value(self) -> "ay":
                return self.value

            @method()
            def ReadValue(self, _options: "a{sv}") -> "ay":
                return self.value

            @method()
            def StartNotify(self):
                self.notifying = True
                self.emit_properties_changed({"Notifying": True})

            @method()
            def StopNotify(self):
                self.notifying = False
                self.emit_properties_changed({"Notifying": False})

            async def notify_value(self, value: bytes) -> None:
                self.value = value
                if self.notifying:
                    self.emit_properties_changed({"Value": value})

        self.interface = Interface()
        self.notify_value = self.interface.notify_value


class BlueChatAdvertisement:
    def __init__(self, name: str) -> None:
        from dbus_next.service import PropertyAccess, ServiceInterface, dbus_property, method

        class Interface(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.bluez.LEAdvertisement1")

            @dbus_property(access=PropertyAccess.READ)
            def Type(self) -> "s":
                return "peripheral"

            @dbus_property(access=PropertyAccess.READ)
            def ServiceUUIDs(self) -> "as":
                return [BLUECHAT_SERVICE_UUID]

            @dbus_property(access=PropertyAccess.READ)
            def LocalName(self) -> "s":
                # Legacy advertising carries the 128-bit service UUID already;
                # keep the human-readable name short enough for the 31-byte budget.
                label = name.encode("utf-8")[:8].decode("utf-8", "ignore")
                return label or "BlueChat"

            @method()
            def Release(self):
                return None

        self.interface = Interface()


class BlueZGattServer:
    def __init__(self, *, max_peers: int = 4) -> None:
        self.max_peers = max_peers
        self.bus: Any = None
        self.adapter_path: str | None = None
        self._advertisement: BlueChatAdvertisement | None = None
        self._object_manager = BlueChatObjectManager()
        self._service = BlueChatGattService()
        self.tx = BlueChatTxCharacteristic()
        self._rx = BlueChatRxCharacteristic(self)
        self._peers: dict[str, BlueZPeerConnection] = {}
        self._accepted: asyncio.Queue[BlueZPeerConnection] = asyncio.Queue()
        self._host_failure = asyncio.Event()
        self._device_match_rule = (
            "type='signal',sender='org.bluez',interface='org.freedesktop.DBus.Properties',"
            "member='PropertiesChanged'"
        )

    async def start(self) -> None:
        from dbus_next import BusType
        from dbus_next.aio import MessageBus

        if self.bus is not None:
            raise BluetoothUnavailableError("BlueZ GATT server is already running")
        self._host_failure.clear()
        try:
            self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            await self._set_device_match(add=True)
            self.bus.add_message_handler(self._handle_message)
            objects = await self._managed_objects()
            candidates = [
                path
                for path, interfaces in objects.items()
                if "org.bluez.GattManager1" in interfaces
                and "org.bluez.LEAdvertisingManager1" in interfaces
            ]
            if not candidates:
                raise BluetoothUnavailableError(
                    "BlueZ adapter lacks GATT hosting or advertising support"
                )
            self.adapter_path = candidates[0]
            self.bus.export(APP_PATH, self._object_manager.interface)
            self.bus.export(SERVICE_PATH, self._service.interface)
            self.bus.export(RX_PATH, self._rx.interface)
            self.bus.export(TX_PATH, self.tx.interface)
            manager = await self._adapter_interface("org.bluez.GattManager1")
            await manager.call_register_application(APP_PATH, {})
        except BluetoothUnavailableError:
            await self.stop()
            raise
        except Exception as exc:
            await self.stop()
            raise BluetoothUnavailableError(
                f"Could not start the BlueZ GATT server ({type(exc).__name__}); check BlueZ and system D-Bus permissions"
            ) from exc

    async def advertise(self, name: str) -> None:
        if self.bus is None or self.adapter_path is None:
            raise BluetoothUnavailableError("Start the GATT server before advertising")
        if self._advertisement is not None:
            raise BluetoothUnavailableError("BlueChat advertisement is already active")
        try:
            self._advertisement = BlueChatAdvertisement(name)
            self.bus.export(ADV_PATH, self._advertisement.interface)
            manager = await self._adapter_interface("org.bluez.LEAdvertisingManager1")
            await manager.call_register_advertisement(ADV_PATH, {})
        except Exception as exc:
            self._advertisement = None
            raise BluetoothUnavailableError(
                f"Could not advertise the BlueChat service ({type(exc).__name__})"
            ) from exc

    async def accept(self) -> BlueZPeerConnection:
        return await self._accepted.get()

    def receive_fragment(self, peer_path: str, fragment: bytes) -> None:
        peer = self._peers.get(peer_path)
        if peer is None:
            if len(self._peers) >= self.max_peers:
                logger.warning("BlueZ rejected a new peer because the room transport is full")
                return
            peer = BlueZPeerConnection(peer_path, self.tx)
            self._peers[peer_path] = peer
            try:
                self._accepted.put_nowait(peer)
            except asyncio.QueueFull:
                self._peers.pop(peer_path, None)
                return
        try:
            peer.feed_fragment(fragment)
        except ProtocolError:
            logger.warning("BlueZ peer sent an invalid GATT fragment")
            peer.mark_disconnected()
            self._peers.pop(peer_path, None)
            asyncio.create_task(self.disconnect(peer_path))

    def _handle_message(self, message: Any) -> bool:
        from dbus_next import MessageType

        if (
            message.message_type != MessageType.SIGNAL
            or message.interface != "org.freedesktop.DBus.Properties"
            or message.member != "PropertiesChanged"
            or not message.body
        ):
            return False
        changed = message.body[1] if len(message.body) > 1 else {}
        changed = changed if isinstance(changed, dict) else {}
        if message.path == self.adapter_path and message.body[0] == "org.bluez.Adapter1":
            powered = changed.get("Powered")
            if powered is not None and getattr(powered, "value", powered) is False:
                self._host_failure.set()
            return False
        if message.body[0] != "org.bluez.Device1":
            return False
        connected = changed.get("Connected")
        if connected is not None and getattr(connected, "value", connected) is False:
            peer = self._peers.get(message.path)
            if peer is not None:
                peer.mark_disconnected()
                self._peers.pop(message.path, None)
        return False

    async def stop_advertising(self) -> None:
        if self.bus is not None and self.adapter_path and self._advertisement is not None:
            try:
                manager = await self._adapter_interface("org.bluez.LEAdvertisingManager1")
                await manager.call_unregister_advertisement(ADV_PATH)
                self.bus.unexport(ADV_PATH, self._advertisement.interface)
            except Exception as exc:
                logger.debug("BlueZ advertisement cleanup failed (%s)", type(exc).__name__)
            self._advertisement = None

    async def wait_host_failure(self) -> None:
        await self._host_failure.wait()

    async def stop(self) -> None:
        if self.bus is None:
            return
        await self.stop_advertising()
        if self.adapter_path:
            try:
                manager = await self._adapter_interface("org.bluez.GattManager1")
                await manager.call_unregister_application(APP_PATH)
            except Exception as exc:
                logger.debug("BlueZ GATT unregister failed (%s)", type(exc).__name__)
        for peer in tuple(self._peers.values()):
            peer.mark_disconnected()
        self._peers.clear()
        while not self._accepted.empty():
            try:
                self._accepted.get_nowait().mark_disconnected()
            except asyncio.QueueEmpty:
                break
        for path, interface in (
            (TX_PATH, self.tx.interface),
            (RX_PATH, self._rx.interface),
            (SERVICE_PATH, self._service.interface),
            (APP_PATH, self._object_manager.interface),
        ):
            try:
                self.bus.unexport(path, interface)
            except Exception:
                pass
        try:
            await self._set_device_match(add=False)
        except Exception as exc:
            logger.debug("BlueZ signal match cleanup failed (%s)", type(exc).__name__)
        self.bus.disconnect()
        self.bus = None
        self.adapter_path = None

    async def disconnect(self, peer_id: str) -> None:
        peer = self._peers.pop(peer_id, None)
        if peer is not None:
            await peer.close()
        try:
            from dbus_next import BusType
            from dbus_next.aio import MessageBus

            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            obj = await bus.introspect(BLUEZ, peer_id)
            proxy = bus.get_proxy_object(BLUEZ, peer_id, obj)
            device = cast(Any, proxy.get_interface("org.bluez.Device1"))
            await device.call_disconnect()
            bus.disconnect()
        except Exception as exc:
            logger.debug("BlueZ device disconnect request failed (%s)", type(exc).__name__)

    async def _managed_objects(self) -> dict[str, dict[str, Any]]:
        root = await self.bus.introspect(BLUEZ, "/")
        proxy = self.bus.get_proxy_object(BLUEZ, "/", root)
        object_manager = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        return await object_manager.call_get_managed_objects()

    async def _adapter_interface(self, interface_name: str) -> Any:
        if self.adapter_path is None:
            raise BluetoothUnavailableError("BlueZ adapter is not selected")
        introspection = await self.bus.introspect(BLUEZ, self.adapter_path)
        proxy = self.bus.get_proxy_object(BLUEZ, self.adapter_path, introspection)
        return proxy.get_interface(interface_name)

    async def _set_device_match(self, *, add: bool) -> None:
        from dbus_next import Message, MessageType

        reply = await self.bus.call(
            Message(
                destination="org.freedesktop.DBus",
                path="/org/freedesktop/DBus",
                interface="org.freedesktop.DBus",
                member="AddMatch" if add else "RemoveMatch",
                signature="s",
                body=[self._device_match_rule],
            )
        )
        if reply.message_type == MessageType.ERROR:
            raise BluetoothUnavailableError(
                "Could not subscribe to BlueZ connection status changes"
            )


class LinuxBluetoothBackend(BleakCentralTransport):
    """BlueZ D-Bus GATT server and Bleak GATT central/client."""

    def __init__(self) -> None:
        super().__init__(can_pair=True)
        self._server = BlueZGattServer()

    @property
    def capabilities(self) -> BluetoothCapabilities:
        return BluetoothCapabilities(
            discovery=True,
            advertising=True,
            hosting=True,
            pairing=True,
            multiple_peers=True,
            max_peers=self._server.max_peers,
        )

    async def is_available(self) -> bool:
        try:
            from dbus_next import BusType
            from dbus_next.aio import MessageBus

            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            objects = await self._read_objects(bus)
            bus.disconnect()
            return any("org.bluez.Adapter1" in interfaces for interfaces in objects.values())
        except Exception as exc:
            logger.debug("BlueZ availability check failed (%s)", type(exc).__name__)
            if is_bluetooth_permission_error(exc):
                raise BluetoothPermissionError(
                    "The system denied access to BlueZ D-Bus. Check your Bluetooth permissions."
                ) from exc
            return False

    async def is_enabled(self) -> bool:
        try:
            from dbus_next import BusType
            from dbus_next.aio import MessageBus

            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            objects = await self._read_objects(bus)
            powered = [
                properties.get("Powered")
                for interfaces in objects.values()
                if (properties := interfaces.get("org.bluez.Adapter1")) is not None
            ]
            bus.disconnect()
            return any(getattr(value, "value", value) is True for value in powered)
        except Exception as exc:
            logger.debug("BlueZ powered-state query failed (%s)", type(exc).__name__)
            if is_bluetooth_permission_error(exc):
                raise BluetoothPermissionError(
                    "The system denied access to the BlueZ adapter state. Check your Bluetooth permissions."
                ) from exc
            return False

    async def discover(self, timeout: float = 8.0) -> list[BluetoothDevice]:
        return await super().discover(timeout)

    async def start_server(self) -> None:
        await self._server.start()

    async def wait_host_failure(self) -> None:
        await self._server.wait_host_failure()

    async def advertise(self, room_metadata: dict[str, Any]) -> None:
        display_name = str(room_metadata.get("device_name", "BlueChat"))
        await self._server.advertise(display_name)

    async def stop_advertising(self) -> None:
        await self._server.stop_advertising()

    async def accept(self) -> Connection:
        return await self._server.accept()

    async def stop_server(self) -> None:
        await self._server.stop()

    async def disconnect(self, peer_id: str) -> None:
        await self._server.disconnect(peer_id)
        await super().disconnect(peer_id)

    @staticmethod
    async def _read_objects(bus: Any) -> dict[str, dict[str, Any]]:
        root = await bus.introspect(BLUEZ, ROOT)
        proxy = bus.get_proxy_object(BLUEZ, ROOT, root)
        manager = proxy.get_interface("org.freedesktop.DBus.ObjectManager")
        return await manager.call_get_managed_objects()
