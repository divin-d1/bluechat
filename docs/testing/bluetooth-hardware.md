# Bluetooth hardware testing

The automated suite exercises protocol behavior with fake connections. It does
not certify radio discovery, advertising, permissions, controller concurrency,
or cross-OS GATT interoperability. Do not mark a backend stable until a row has
been run on physical machines and the result includes OS version, adapter, and
BlueZ/driver details.

## Test procedure

1. Install BlueChat on two nearby computers. Do not join either to Wi-Fi; this
   verifies the no-Internet path.
2. Confirm Bluetooth is enabled and the adapter supports BLE peripheral mode on
   the host and central mode on the client.
3. On the host run `bluechat host`. Confirm BlueChat service advertisement and
   the five-minute countdown.
4. On the client run `bluechat devices`, confirm the host is marked BlueChat
   Host, then run `bluechat join` and select it.
5. Submit the current six-character room code. Confirm wrong/expired codes fail
   before approval and the host receives a username approval prompt.
6. Approve the client. Exchange at least 100 short messages in both directions,
   including while each side is typing. Verify message order and that no input
   line is corrupted.
7. Reject another join, disconnect one side, and repeat a join. Record whether
   the radio/OS requires pairing or a Bluetooth settings confirmation.
8. Stop the room and confirm advertising is removed and the peripheral can be
   scanned again after restart.

## Current matrix

`NOT_RUN` means no physical cross-machine validation has been performed. Linux,
Windows, and macOS host backends are implemented in code. Host API availability,
permissions, and real interoperability remain pending validation on the target OS.

| Host OS | Client OS | Host/client code | Automated tests | Discovery | Connect/auth/chat | Files/reconnect | Hardware result |
|---|---|---|---|---|---|---|---|
| Linux | Linux | IMPLEMENTED | AUTOMATED TESTED (backend contracts) | PENDING | PENDING | PENDING | NOT_RUN |
| Linux | Windows | IMPLEMENTED | AUTOMATED TESTED (backend contracts) | PENDING | PENDING | PENDING | NOT_RUN |
| Linux | macOS | IMPLEMENTED | AUTOMATED TESTED (backend contracts) | PENDING | PENDING | PENDING | NOT_RUN |
| Windows | Linux | IMPLEMENTED | AUTOMATED TESTED (backend contracts) | PENDING | PENDING | PENDING | NOT_RUN |
| Windows | Windows | IMPLEMENTED | AUTOMATED TESTED (backend contracts) | PENDING | PENDING | PENDING | NOT_RUN |
| Windows | macOS | IMPLEMENTED | AUTOMATED TESTED (backend contracts) | PENDING | PENDING | PENDING | NOT_RUN |
| macOS | Linux | IMPLEMENTED | AUTOMATED TESTED (backend contracts) | PENDING | PENDING | PENDING | NOT_RUN |
| macOS | Windows | IMPLEMENTED | AUTOMATED TESTED (backend contracts) | PENDING | PENDING | PENDING | NOT_RUN |
| macOS | macOS | IMPLEMENTED | AUTOMATED TESTED (backend contracts) | PENDING | PENDING | PENDING | NOT_RUN |

Application-level fake transport tests cover group text routing, private and
group file transfer, secure room-code PAKE, and live private/non-host group
session resumption with single-use credentials. Native API lifecycle tests mock
BlueZ, WinRT, and CoreBluetooth callbacks. The local suite currently has 60
passing tests. Fake transport tests exercise host service restart, room-state
retention, and timeout termination; platform lifecycle mocks exercise native
failure signals. These tests do not certify real BLE recovery or cross-platform
radio behavior. See `BLUECHAT_GOALS.md`.

## Troubleshooting notes to record

- Whether BlueZ exposes `GattManager1` and `LEAdvertisingManager1` on the
  selected adapter.
- Whether another process owns the adapter or an advertisement slot.
- Adapter controller model and the number of concurrent central connections
  it accepts.
- Whether macOS Bluetooth privacy permission or Windows Bluetooth capability
  settings blocked the scan/connect operation.
- Negotiated ATT MTU and the observed time to deliver a 1 KiB protocol packet.
- Disconnect behavior when moving out of range, toggling Bluetooth, and
  terminating BlueChat normally.
