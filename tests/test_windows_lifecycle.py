from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from uuid import UUID

from bluechat.bluetooth.gatt import BLUECHAT_RX_UUID, BLUECHAT_TX_UUID, fragment_packet
from bluechat.bluetooth.windows_peripheral import WindowsGattPeripheral


def _install_winrt_fakes(monkeypatch):
    class Characteristic:
        def __init__(self) -> None:
            self.subscribed_clients = []

        def add_write_requested(self, callback):
            self.write_callback = callback

        def add_subscribed_clients_changed(self, callback):
            self.subscriber_callback = callback

        async def notify_value_for_subscribed_client_async(self, buffer, _client):
            self.notification = bytes(buffer)
            return SimpleNamespace(status="Success")

    class Service:
        async def create_characteristic_async(self, identifier, _parameters):
            characteristic = Characteristic()
            characteristics[identifier] = characteristic
            return SimpleNamespace(error="Success", characteristic=characteristic)

    class Provider:
        def __init__(self) -> None:
            self.service = Service()
            self.advertisement_status = "Stopped"

        def add_advertisement_status_changed(self, callback):
            self.status_callback = callback

        def start_advertising_with_parameters(self, _parameters):
            self.advertisement_status = "Started"
            self.status_callback(self, None)

        def stop_advertising(self):
            self.advertisement_status = "Stopped"
            self.stopped = True

    class ProviderFactory:
        @staticmethod
        async def create_async(_identifier):
            provider = Provider()
            providers.append(provider)
            return SimpleNamespace(error="Success", service_provider=provider)

    class DataReader:
        @staticmethod
        def from_buffer(buffer):
            return SimpleNamespace(
                unconsumed_buffer_length=len(buffer),
                read_bytes=lambda target: target.__setitem__(slice(None), buffer),
            )

    class DataWriter:
        def __init__(self) -> None:
            self.value = b""

        def write_bytes(self, data):
            self.value = bytes(data)

        def detach_buffer(self):
            return self.value

    characteristics = {}
    providers = []
    adapter_module = ModuleType("winrt.windows.devices.bluetooth")

    class BluetoothAdapter:
        @staticmethod
        async def get_default_async():
            return SimpleNamespace(is_low_energy_supported=True, is_peripheral_role_supported=True)

    adapter_module.BluetoothAdapter = BluetoothAdapter
    gatt_module = ModuleType("winrt.windows.devices.bluetooth.genericattributeprofile")
    gatt_module.GattCharacteristicProperties = SimpleNamespace(WRITE=8, INDICATE=32)
    gatt_module.GattProtectionLevel = SimpleNamespace(PLAIN=0)
    gatt_module.GattCommunicationStatus = SimpleNamespace(SUCCESS="Success")
    gatt_module.GattLocalCharacteristicParameters = type("Parameters", (), {})
    gatt_module.GattServiceProvider = ProviderFactory
    gatt_module.GattServiceProviderAdvertisingParameters = type("AdvertisingParameters", (), {})
    streams_module = ModuleType("winrt.windows.storage.streams")
    streams_module.DataReader = DataReader
    streams_module.DataWriter = DataWriter
    module_names = (
        "winrt",
        "winrt.windows",
        "winrt.windows.devices",
        "winrt.windows.devices.bluetooth",
        "winrt.windows.devices.bluetooth.genericattributeprofile",
        "winrt.windows.storage",
        "winrt.windows.storage.streams",
    )
    for name in module_names:
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    monkeypatch.setitem(sys.modules, "winrt.windows.devices.bluetooth", adapter_module)
    monkeypatch.setitem(
        sys.modules,
        "winrt.windows.devices.bluetooth.genericattributeprofile",
        gatt_module,
    )
    monkeypatch.setitem(sys.modules, "winrt.windows.storage.streams", streams_module)
    return characteristics, providers


def test_winrt_provider_write_notify_disconnect_and_cleanup(monkeypatch) -> None:
    characteristics, providers = _install_winrt_fakes(monkeypatch)

    async def run() -> None:
        peripheral = WindowsGattPeripheral()
        await peripheral.start()
        await peripheral.advertise()
        provider = providers[0]
        assert provider.advertisement_status == "Started"
        assert set(characteristics) == {UUID(BLUECHAT_RX_UUID), UUID(BLUECHAT_TX_UUID)}

        client = SimpleNamespace(session=SimpleNamespace(device_id=SimpleNamespace(id="peer-7")))
        tx = characteristics[UUID(BLUECHAT_TX_UUID)]
        tx.subscribed_clients = [client]
        peripheral._apply_subscribers([client])
        connection = await peripheral.accept()

        class Request:
            offset = 0
            value = fragment_packet(b"client-in")[0]

            def __init__(self) -> None:
                self.responded = False

            def respond(self):
                self.responded = True

            def respond_with_protocol_error(self, error):
                self.error = error

        request = Request()
        args = SimpleNamespace(
            session=client.session,
            get_request_async=lambda: asyncio.sleep(0, result=request),
        )

        class Deferral:
            completed = False

            def complete(self):
                self.completed = True

        deferral = Deferral()
        await peripheral._process_write(args, deferral)
        assert request.responded and deferral.completed
        assert await connection.receive() == b"client-in"

        await connection.send(b"host-to-client")
        assert tx.notification
        assert provider.advertisement_status == "Started"
        provider.advertisement_status = "Aborted"
        peripheral._advertisement_status_changed(provider, None)
        await asyncio.wait_for(peripheral.wait_host_failure(), timeout=1)

        invalid_request = Request()
        invalid_request.offset = 1
        invalid_args = SimpleNamespace(
            session=client.session,
            get_request_async=lambda: asyncio.sleep(0, result=invalid_request),
        )
        rejected_deferral = Deferral()
        await peripheral._process_write(invalid_args, rejected_deferral)
        assert invalid_request.error == 0x07
        assert rejected_deferral.completed

        peripheral._apply_subscribers([])
        try:
            await connection.receive()
        except ConnectionError:
            pass
        else:
            raise AssertionError("Removing a WinRT subscriber did not close its connection")

        await peripheral.stop()
        assert provider.stopped
        assert peripheral._provider is None

    asyncio.run(run())
