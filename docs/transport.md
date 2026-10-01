# Bluetooth transport decision

## Selected transport

BlueChat uses BLE GATT rather than RFCOMM. RFCOMM works well in some desktop
stacks but does not expose a consistent host/server API across Linux, Windows,
and macOS. BLE GATT gives BlueChat a central/client role and a peripheral/server
role with the same service model. Implementations remain OS-specific behind
`BluetoothTransport`.

The BlueChat custom service is `a64e2f20-8d8a-4f1b-a5cc-5d64b5c0a100`:

| Characteristic | Direction from client | GATT properties | Purpose |
|---|---|---|---|
| RX `...a101` | client → host | write, write without response | Fragments of BlueChat packet |
| TX `...a102` | host → client | indicate | Fragments of BlueChat packet |

We use one RX/TX packet pipe rather than separate control, message, and file
characteristics. Protocol types and framing already describe those operations;
keeping GATT as a byte transport avoids duplicating protocol semantics in each
platform backend. `...a103` is reserved and not currently exposed.

## MTU, delivery, and limits

The BLE default ATT MTU is 23 bytes. ATT write/notification/indication values
therefore start with a 20-byte payload ceiling. Current transport fragments use
a 10-byte BlueChat header and up to 10 bytes of application packet data per
fragment, which is conservative and compatible with that baseline. Client writes
use write-with-response; host TX is indication-only so GATT confirmation
provides backpressure for each fragment. This is intentionally reliable but
slow. Future file transfer needs negotiated limits, a bounded priority queue,
and throughput tests before changing fragment sizes.

Reassembly permits out-of-order chunks, caps concurrent incomplete frames, and
rejects conflicting duplicates and invalid headers. BlueChat's own 4-byte frame
length and encryption remain above this layer. The present GATT implementation
limits fragments to a 16-bit chunk count; large packets beyond that capacity are
refused before transmission.

GATT indications and subscriptions can be shared by connected centrals on the
Linux server, but actual simultaneous peer counts depend on the controller and
BlueZ. The current backend sets a software ceiling of four guests, subject to
hardware verification. Host group routing is not implemented yet.

## Backend roles

| OS | Discover/client | Advertise/server | Pairing |
|---|---|---|---|
| Linux | Bleak | BlueZ GATT Manager + LE Advertising Manager over system D-Bus | Bleak/BlueZ where supported |
| Windows | Bleak WinRT client | WinRT `GattServiceProvider` | Bleak WinRT where supported |
| macOS | Bleak CoreBluetooth client | CoreBluetooth `CBPeripheralManager` through PyObjC | Managed by CoreBluetooth/OS |

BlueZ owns adapter discovery and state via D-Bus `Adapter1`; local GATT service
registration is done using `GattManager1.RegisterApplication`; advertising uses
`LEAdvertisingManager1.RegisterAdvertisement`. No command output parsing is
used. GATT clients use Bleak's scanner, client, writes, and notify/indicate
subscription APIs. Windows hosting uses the WinRT `GattServiceProvider` with
local write/indicate characteristics. macOS uses CoreBluetooth's
`CBPeripheralManager` through PyObjC. Their native callback APIs stay inside
their respective modules and expose only the common `Connection` interface to
application code. All three hosting implementations still require physical
interoperability tests.

The cross-platform contract is in `bluetooth/base.py`. OS objects stay in their
backend implementation and are represented above it only by
`BluetoothDevice`, `Connection`, `PairResult`, and `BluetoothCapabilities`.

## References

- [BlueZ GattManager D-Bus API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.GattManager.rst)
- [BlueZ LEAdvertisingManager D-Bus API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.LEAdvertisingManager.rst)
- [BlueZ Adapter D-Bus API](https://github.com/bluez/bluez/blob/master/doc/org.bluez.Adapter.rst)
- [Bleak scanner API](https://bleak.readthedocs.io/en/latest/api/scanner.html)
- [Bleak client API](https://bleak.readthedocs.io/en/latest/api/client.html)
- [Apple CoreBluetooth](https://developer.apple.com/documentation/corebluetooth)
- [Windows GATT service provider](https://learn.microsoft.com/en-us/uwp/api/windows.devices.bluetooth.genericattributeprofile.gattserviceprovider)
