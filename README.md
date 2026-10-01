# BlueChat

BlueChat is an open-source, terminal-first project for direct chat between
nearby computers over Bluetooth. Internet access is not part of the design.

> **Development status:** BlueChat has a BLE GATT path with a BlueZ D-Bus
> server/advertiser on Linux and Bleak GATT client operations on Linux, Windows,
> and macOS. Native peripheral paths now exist for Linux (BlueZ), Windows
> (WinRT), and macOS (CoreBluetooth). The terminal host/join path performs room-code authentication, host
> approval, encrypted text chat, host-routed group messaging, and a five-minute
> joining-code countdown. Private file transfer streams with recipient approval
> and SHA-256 verification. Automated tests cover these application services,
> but native transport interoperability has not been validated on physical
> computers. Group file offers now use host relaying and independent recipient
> approvals. Private and non-host group peers can resume a dropped session
> within 30 seconds using a single-use, room-bound credential. Recovery after
> loss of the host's native GATT service restarts the advertiser and preserves
> room state within the same 30-second window. Recovery has fake/native-mock
> coverage but still needs real-radio validation.
> Room authentication uses SPAKE2, which has not been independently audited.
> Bluetooth interoperability is pending real hardware tests; this is not a
> stable release.

## Install

Python 3.10 or newer is required.

The TestPyPI name `bluechat` is already used by an unrelated project, so this
preview distribution is named `bluechat-terminal`. Its executable and Python
import remain `bluechat`. After the preview upload, install it with:

```sh
python -m pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  bluechat-terminal==0.1.0
```

The second index supplies BlueChat's dependencies, which are not mirrored to
TestPyPI. The public PyPI name `bluechat` is currently available; once a
stable release is approved there, the normal install will be
`python -m pip install bluechat`.

For development from a checkout:

```sh
python -m venv .venv
. .venv/bin/activate                 # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"   # Windows PowerShell uses the same command
bluechat --help
```

On first run, BlueChat asks for a local username and saves it in the
platform-appropriate application configuration directory.

## Quick start

Start the interactive terminal menu:

```sh
bluechat
```

Or start a room directly:

```sh
bluechat host                 # private room
bluechat host --group         # group room, up to five people total
bluechat join                 # discover and join a nearby BlueChat host
bluechat devices              # list nearby BLE devices
bluechat doctor               # inspect local Bluetooth/backend readiness
bluechat config               # view or change local settings
```

The host shares a temporary six-character room code. The code expires after
five minutes; the host approves every join request. In a room, use `/help` to
see chat commands, including `/who`, `/info`, `/newcode`, and `/send <path>`.

`bluechat host` starts a private room; add `--group` for a group room. The
native GATT host for the current operating system is:
BlueZ on Linux, WinRT on Windows, and CoreBluetooth on macOS. Each publishes the
same BlueChat service and waits up to five minutes for a guest.
`bluechat join` scans BLE devices, selects a BlueChat advertiser, requests its
code, and opens an encrypted chat. `bluechat devices` lists nearby BLE devices;
`bluechat config username` edits the saved username; `bluechat info` shows
implementation status; `bluechat doctor` reports adapter/backend diagnostics;
and `bluechat --version` shows the installed version.

Group rooms route text through the host and support up to five total members.
Private and group sessions support `/send <path>` with independent recipient
approval, bounded chunk streaming, and SHA-256 verification. Group transfers
are relayed by the host only to accepted participants; the host can offer a
file independently to each connected member. Each group participant, including
the host, controls local history independently (`ask`, `always`, or `never`).
Unexpected private and group participant disconnects have a 30-second automatic
resume window. The host also attempts to restart its BLE GATT service after a
native failure; room state remains in memory during that window. Real radio
recovery remains pending hardware tests.

All three systems now have both host and client code paths, but physical
interoperability is pending hardware testing on every OS pair. The scan covers
BLE devices, not classic-only Bluetooth devices. Pairing remains OS-managed or
optional for GATT connections; application room authentication is separate
from OS pairing.

## Developer API

The CLI and importable API share the same `BlueChat` service facade:

```python
from bluechat import BlueChat

app = BlueChat(username="Divin")
room = app.create_room(group=False)
print(room.code)  # six characters, expires after five minutes
print(room.remaining_seconds())
```

The memory transport tests encrypted bidirectional messages without hardware:

```python
guest_id, host, guest = app.join_memory_room(room, room.code, username="Alex", approved=True)
guest.send_text("Hello")
print(host.receive(timeout=1))
host.close()
guest.close()
```

## Architecture

For the complete system overview—with architecture and sequence diagrams,
protocol layering, group routing, transfers, and security boundaries—see
[`ARCHITECTURE.md`](ARCHITECTURE.md).

- `bluetooth/base.py` defines the asynchronous, platform-neutral transport,
  device, connection, capability, and pairing-result contracts.
- `bluetooth/bleak_central.py` provides BLE scanning and client connections.
- `bluetooth/linux.py` implements BlueZ D-Bus adapter checks, GATT server
  registration, advertising, peer acceptance, and notifications/indications.
- `bluetooth/windows_peripheral.py` implements the WinRT `GattServiceProvider`
  GATT server and bridges native writes/notifications to asyncio connections.
- `bluetooth/macos_peripheral.py` implements a CoreBluetooth GATT peripheral,
  with Objective-C delegates bridged back to the asyncio loop.
- `bluetooth/windows.py` and `bluetooth/macos.py` select native server and
  cross-platform Bleak client implementations behind the common transport.
- `bluetooth/gatt.py` defines a custom service UUID with RX write and TX
  indication characteristics. Chat and control stay in the versioned protocol,
  not in GATT-specific command characteristics.
- `chat/` manages room expiry, stable participant IDs, approval, bounded
  host-side group routing, and asynchronous encrypted sessions.
- `protocol/` uses strict versioned JSON control messages and 4-byte
  length-prefixed frames.
- `security/` uses the `spake2` PAKE implementation for room-code mutual
  authentication, HKDF-SHA256 for directional keys, and `cryptography` AES-GCM
  for authenticated records. An expiring single-use resume-token registry and
  secure resume handshake support private/group participant resumption and
  host-service restart within a 30-second recovery window.
- `transfer/` contains safe destination handling and acknowledged streaming
  transfer protocols for private and host-relayed group file sharing.
- `history/` stores participant-controlled local JSONL records.
- `config/` stores human-readable TOML in the OS configuration directory from
  `platformdirs`.

## Why BLE GATT

RFCOMM does not provide a uniform server API across the three desktop operating
systems. BlueChat uses BLE GATT for a common central/client model, while keeping
hosting in native peripheral/server adapters. BlueZ documents D-Bus registration
for external GATT services and LE advertisements. Bleak provides the client
half, not a general host/server. Linux hosting uses BlueZ, Windows uses
WinRT `GattServiceProvider`, and macOS uses CoreBluetooth. All three server
paths are implemented but await physical interoperability testing. See
[`docs/transport.md`](docs/transport.md) for details and source references.

GATT's minimum ATT MTU is 23 bytes, so packets are fragmented into conservative
chunks, acknowledged on client writes and host indications, then reassembled
before BlueChat framing. It supports text/control frames and bounded file
chunks. Group text fan-out is implemented above the transport; simultaneous
connection limits and throughput still need physical validation.

## Security limits

The room code alphabet excludes confusing characters and codes are generated
with `secrets`. Room authentication uses the maintained `spake2` package's
SPAKE2 PAKE, followed by transcript-bound mutual key confirmation. A passive
packet capture does not provide an offline verifier for guesses; active peers
can make online guesses, which the host rate-limits. The Python PAKE package
warns that its operations are not constant-time, so a sufficiently capable
nearby timing observer may gain information. The exchange and BlueChat
integration have not received an independent security audit. Group members
have separate encrypted links to the host, which can read and route plaintext;
group chat is not end-to-end encrypted between clients. AES-GCM authenticates
records and rejects modified or replayed packets. Resume credentials are
room-, session-, and participant-bound, consumed on use, and rotated after a
fresh X25519 exchange. No keys, room codes, or plaintext messages are logged.

## Unexpected input and failure behavior

Usernames are bounded to 32 characters and reject Unicode control/format
characters. Room codes must match the allowed six-character alphabet. Config
types and keys are checked; damaged TOML is backed up to `config.toml.corrupt`
and safe defaults are used. Protocol decoding rejects unsupported versions,
message types, unknown fields, malformed JSON, and oversized metadata. Framing
and GATT fragmentation enforce limits and reject invalid lengths, duplicate
conflicts, and inconsistent chunk counts. Invalid encrypted records fail the
session closed. Expected OS/backend failures become readable CLI errors;
`--debug` enables diagnostics.

## Development

```sh
python -m pip install -e ".[dev]"
pytest
ruff check .
```

Fake transport tests do not require Bluetooth hardware. Physical validation
instructions and the cross-platform test matrix are in
[`docs/testing/bluetooth-hardware.md`](docs/testing/bluetooth-hardware.md).
See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

Release preparation and TestPyPI validation steps are in
[`docs/releasing.md`](docs/releasing.md).

## Roadmap

1. Run the configured CI matrix on Linux, Windows, and macOS.
2. Hardware-test the native Linux, Windows, and macOS host/client paths.
3. Independently audit the SPAKE2 integration before a stable release.

## License

MIT. See [LICENSE](LICENSE).
