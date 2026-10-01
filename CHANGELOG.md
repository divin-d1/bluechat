# Changelog

## 0.1.0 — TestPyPI preview candidate (`bluechat-terminal` distribution)

- Establish the package, configuration, terminal CLI, async transport contract,
  protocol framing, and in-memory development transport.
- Add BlueZ D-Bus Linux GATT server/advertising and Bleak client discovery,
  connection, write, indication, pairing, and disconnect handling.
- Wire room-code authentication, host approval, encrypted private chat, and the
  five-minute room-code countdown into the terminal host/join flow.
- Add host-routed group text with up to five participants, stable IDs, duplicate
  suppression, bounded peer queues, join/leave status, `/who`, `/info`, and
  group-host `/newcode`.
- Add receiver-approved acknowledged file streaming, SHA-256 validation,
  filename sanitization, duplicate-safe destinations, and local JSONL history.
- Add group file offer relay with independent recipient decisions, bounded
  per-recipient acknowledgement deadlines, host-side sending, and group-host
  local history.
- Expand session information with host, capacity, local-history status, and
  monotonic session duration.
- Add live private and non-host group reconnect with bounded 30-second retries,
  suspended single-use resume credentials, fresh X25519-derived channel keys,
  roster restoration, and timeout cleanup. Add host GATT service recovery after
  native adapter/advertisement failure, retaining the in-memory room and ending
  it cleanly when the 30-second recovery deadline expires.
- Add native lifecycle mocks for BlueZ, WinRT, and CoreBluetooth, including
  service registration, advertising, GATT writes/notifications, disconnects,
  permission/capability failures, and cleanup.
- Implement Linux BlueZ, Windows WinRT, and macOS CoreBluetooth host paths;
  physical cross-platform validation remains pending.
