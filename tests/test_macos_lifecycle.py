from __future__ import annotations

import asyncio
import sys
from types import ModuleType

from bluechat.bluetooth.gatt import BLUECHAT_RX_UUID, fragment_packet
from bluechat.bluetooth.macos_peripheral import CoreBluetoothPeripheral
from bluechat.errors import BluetoothUnavailableError


def _install_corebluetooth_fakes(monkeypatch, *, initial_state: int = 5):
    class NSObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    class UUID:
        def __init__(self, value: str) -> None:
            self.value = value.lower()

        @classmethod
        def UUIDWithString_(cls, value: str):
            return cls(value)

        def UUIDString(self) -> str:
            return self.value

    class Characteristic:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithType_properties_value_permissions_(self, uuid, *_args):
            self.uuid = uuid
            return self

        def UUID(self):
            return self.uuid

    class Service:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithType_primary_(self, uuid, _primary):
            self.uuid = uuid
            return self

        def setCharacteristics_(self, characteristics):
            self.characteristics = characteristics

    class PeripheralManager:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithDelegate_queue_options_(self, delegate, _queue, _options):
            self.delegate = delegate
            self._state = initial_state
            delegate.peripheralManagerDidUpdateState_(self)
            return self

        def state(self):
            return self._state

        def addService_(self, service):
            self.service = service
            self.delegate.peripheralManager_didAddService_error_(self, service, None)

        def startAdvertising_(self, _advertisement):
            self.delegate.peripheralManagerDidStartAdvertising_error_(self, None)

        def stopAdvertising(self):
            self.advertising_stopped = True

        def removeAllServices(self):
            self.services_removed = True

        def respondToRequest_withResult_(self, request, result):
            request.response = result

        def updateValue_forCharacteristic_onSubscribedCentrals_(
            self, value, _characteristic, centrals
        ):
            self.last_notification = (bytes(value), centrals)
            return True

    core = ModuleType("CoreBluetooth")
    constants = {
        "CBAdvertisementDataServiceUUIDsKey": "service-uuids",
        "CBManagerStatePoweredOff": 4,
        "CBManagerStatePoweredOn": 5,
        "CBManagerStateUnauthorized": 3,
        "CBManagerStateUnsupported": 2,
        "CBMutableCharacteristic": Characteristic,
        "CBMutableService": Service,
        "CBCharacteristicPropertyIndicate": 2,
        "CBCharacteristicPropertyWrite": 8,
        "CBAttributePermissionsReadable": 1,
        "CBAttributePermissionsWriteable": 2,
        "CBPeripheralManager": PeripheralManager,
        "CBUUID": UUID,
        "CBATTErrorAttributeNotFound": 10,
        "CBATTErrorInsufficientResources": 17,
        "CBATTErrorSuccess": 0,
    }
    for name, value in constants.items():
        setattr(core, name, value)
    foundation = ModuleType("Foundation")
    foundation.NSObject = NSObject
    foundation.NSData = type(
        "NSData",
        (),
        {"dataWithBytes_length_": staticmethod(lambda data, length: bytes(data[:length]))},
    )
    dispatch = ModuleType("libdispatch")
    dispatch.dispatch_queue_create = lambda *_args: object()
    dispatch.dispatch_async = lambda _queue, callback: callback()
    monkeypatch.setitem(sys.modules, "CoreBluetooth", core)
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setitem(sys.modules, "libdispatch", dispatch)
    return PeripheralManager


def test_corebluetooth_host_start_advertise_write_notify_disconnect_and_cleanup(monkeypatch):
    PeripheralManager = _install_corebluetooth_fakes(monkeypatch)

    class Central:
        def identifier(self):
            return self

        def UUIDString(self):
            return "central-1"

    class Request:
        def __init__(self, central, value):
            self._central = central
            self._value = value

        def characteristic(self):
            return type(
                "Attribute",
                (),
                {
                    "UUID": lambda _self: type(
                        "AttributeUUID", (), {"UUIDString": lambda _self: BLUECHAT_RX_UUID}
                    )()
                },
            )()

        def central(self):
            return self._central

        def value(self):
            return self._value

    async def run() -> None:
        backend = CoreBluetoothPeripheral()
        await asyncio.wait_for(backend.start(), timeout=3)
        await asyncio.wait_for(backend.advertise(), timeout=3)
        native = backend._peripheral
        assert isinstance(native, PeripheralManager)
        assert native.service.characteristics

        central = Central()
        await backend._on_subscribe(central)
        connection = await backend.accept()
        requests = [Request(central, fragment) for fragment in fragment_packet(b"client-to-host")]
        await backend._on_write(native, requests)
        assert all(request.response == 0 for request in requests)
        assert await connection.receive() == b"client-to-host"

        await connection.send(b"host-to-client")
        assert native.last_notification[1] == [central]
        native._state = 4
        native.delegate.peripheralManagerDidUpdateState_(native)
        await asyncio.wait_for(backend.wait_host_failure(), timeout=1)
        await backend._on_unsubscribe(central)
        try:
            await connection.receive()
        except ConnectionError:
            pass
        else:
            raise AssertionError("CoreBluetooth unsubscribe did not close the logical peer")

        await backend.stop()
        assert native.advertising_stopped and native.services_removed
        assert backend._peripheral is None

    asyncio.run(run())


def test_corebluetooth_unauthorized_state_is_reported_and_cleaned(monkeypatch):
    _install_corebluetooth_fakes(monkeypatch, initial_state=3)

    async def run() -> None:
        backend = CoreBluetoothPeripheral()
        try:
            await backend.start()
        except BluetoothUnavailableError as exc:
            assert "System Settings" in str(exc)
        else:
            raise AssertionError("Unauthorized CoreBluetooth state was reported as ready")
        assert backend._peripheral is None

    asyncio.run(run())
