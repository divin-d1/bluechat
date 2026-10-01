# BlueChat V1 Goals

This tracker is authoritative for V1 software work. `DONE` requires passing automated tests for the stated behavior. Hardware results are tracked separately in [`docs/testing/bluetooth-hardware.md`](docs/testing/bluetooth-hardware.md).

## Progress

- Software goals DONE: 24 / 24 (100%).
- Physical hardware validation: separately tracked; no combination may be inferred as passed.
- Latest full local test run: 60 passed (2026-10-01). `ruff check .` and `mypy src/bluechat` pass.
- Wheel and sdist rebuilt from the current tree; clean dependency-resolving installs and installed CLI smoke checks (`--version`, `--help`, `doctor`) pass on Intel macOS / Python 3.11.
- GitHub-hosted CI cannot be triggered from this workspace; cross-platform CI execution remains pending.

## Goals

### G01 Group file transfer

Status: DONE

Acceptance criteria:
- [x] `/send <path>` works for group participants and the host.
- [x] Offers are routed to intended recipients and payload is streamed only after each recipient accepts.
- [x] SHA-256, path safety, bounded streaming, responsive chat, and transfer isolation pass automated integration tests.

Tests: `test_group_file_relay_has_independent_approval_and_integrity`, `test_async_file_transfer_requires_approval_and_verifies_digest`.
Notes: host relays client-originated offers. Host-originated offers are independently sent to active peers. Large data remains chunked and only accepted recipients receive chunks.
Blockers: none identified.

### G02 Per-recipient group file approval

Status: DONE

Acceptance criteria:
- [x] Each group recipient independently accepts or rejects an offer.
- [x] A rejected peer receives no file payload.
- [x] Approval and rejection are covered in multi-peer integration tests.

Tests: `test_group_file_relay_has_independent_approval_and_integrity`.
Notes: transfer relay aggregates each peer's decision; declines do not receive FILE_START or FILE_CHUNK.
Blockers: none.

### G03 Group file relay

Status: DONE

Acceptance criteria:
- [x] Host relays only to accepted recipients.
- [x] Slow or disconnected recipients do not stall other transfers or chat.
- [x] Temporary data is bounded and cleaned on failure/cancellation.
- [x] Multi-recipient integrity and failure-isolation tests pass.

Tests: `test_group_file_relay_has_independent_approval_and_integrity` includes a non-acknowledging recipient while an accepting peer completes and group text routes; `test_complete_group_transfer_reconnect_rotation_history_workflow` covers the combined group lifecycle.
Notes: relay has bounded per-transfer queues, peer acknowledgement deadlines and failure isolation. Receiver temporary-file lifecycle is covered by the existing transfer manager tests.
Blockers: none.

### G04 Live 30-second reconnection

Status: DONE

Acceptance criteria:
- [x] Unexpected transport loss starts a 30-second reconnect window.
- [x] Attempts use bounded retry/backoff and report success/failure.
- [x] Expiry cleans up the participant/session and is tested.

Tests: `test_reconnect_retries_with_backoff_until_window_expires`, `test_live_private_resume_uses_bound_credential_and_restores_chat`, `test_live_group_resume_restores_same_participant_and_fresh_secure_session`, `test_expired_group_reconnect_removes_roster_token_and_records_leave`.
Notes: client retries use bounded backoff against a monotonic 30-second deadline; private and group host paths await secure resumption.
Blockers: none identified.

### G05 Secure session resume integration

Status: DONE

Acceptance criteria:
- [x] Live reconnect uses a room-, participant-, and session-bound short-lived credential.
- [x] Credentials are single-use or otherwise replay-resistant; expired, replayed, and wrong-context credentials fail.
- [x] Resume re-establishes fresh channel keys and passes tamper/replay tests.

Tests: `test_resume_registry_rejects_wrong_session_without_consuming_valid_token`, `test_resume_handshake_derives_fresh_keys_and_rejects_replay`, `test_live_private_resume_uses_bound_credential_and_restores_chat`, `test_live_group_resume_restores_same_participant_and_fresh_secure_session`.
Notes: credentials bind room, participant, and session, are consumed by authenticated resume, rotated, and followed by fresh X25519-derived directional keys.
Blockers: none for private/member resume; host-room service recovery remains G07.

### G06 Non-host group reconnect

Status: DONE

Acceptance criteria:
- [x] A disconnected non-host remains visibly reconnecting for up to 30 seconds.
- [x] Other participants continue exchanging messages during the window.
- [x] Successful resume restores the same participant; timeout removes them and announces leave.

Tests: `test_live_group_resume_restores_same_participant_and_fresh_secure_session`, `test_expired_group_reconnect_removes_roster_token_and_records_leave`, `test_reconnect_retries_with_backoff_until_window_expires`, `test_complete_group_transfer_reconnect_rotation_history_workflow`.
Notes: other group participants remain active; a resumed guest returns with its stable participant ID and a rotated credential.
Blockers: none.

### G07 Host reconnect workflow

Status: DONE

Acceptance criteria:
- [x] Host transport loss enters a 30-second room-resume window.
- [x] Host returns with secure session resumption and room state intact.
- [x] Retry, cleanup, and failure cases pass automated tests.

Tests: `test_host_transport_recovers_service_and_retains_room_state`, `test_blocked_host_accept_rebinds_after_server_restart`, `test_live_private_resume_uses_bound_credential_and_restores_chat`, `test_live_group_resume_restores_same_participant_and_fresh_secure_session`.
Notes: native host failure events restart the GATT service and advertisement within 30 seconds; the same in-memory room, participant IDs, code, and resume registry remain active. Native mock tests exercise BlueZ, WinRT, and CoreBluetooth failure signals; physical radio recovery remains unverified.
Blockers: none for software behavior.

### G08 Host-loss room termination

Status: DONE

Acceptance criteria:
- [x] Members receive a host-disconnected state while awaiting resume.
- [x] Room terminates cleanly when the host misses the deadline.
- [x] A successful host resume restores the room; no host migration is attempted.

Tests: `test_host_service_recovery_timeout_ends_room`, `test_reconnect_retries_with_backoff_until_window_expires`, `test_private_host_resume_window_expiry_returns_no_session`.
Notes: service timeout sets the room-ended event consumed by the group UI; clients show the host loss and end their local room after the secure 30-second resume attempt. Host migration is not implemented.
Blockers: none for the tested software lifecycle.

### G09 Host-side group history

Status: DONE

Acceptance criteria:
- [x] Host `ask`/`always`/`never` preference is honored independently.
- [x] Messages, join/leave events, and benign file metadata are recorded when enabled.
- [x] History failures never terminate a room and no secrets are recorded.

Tests: `test_history_is_local_preference_controlled_and_sanitized` covers ask/always/never, message/join/leave/file events, path-safe peer names, and write failures; `test_complete_group_transfer_reconnect_rotation_history_workflow` exercises history during a live group workflow. The group-host prompt records those events through the same local-only manager.
Notes: the host asks independently from every guest; local history is never required by room policy.
Blockers: none identified.

### G10 Complete `/who`

Status: DONE

Acceptance criteria:
- [x] Private and group sessions show active participants and reconnecting state.
- [x] Participant names update on join, leave, disconnect, and reconnect.
- [x] Internal IDs/secrets are not shown in normal UI.
- [x] CLI/session tests cover private and group views.

Tests: `test_session_info_contains_runtime_facts_without_room_secrets` also covers `_format_who` host labeling, reconnecting state, and hiding identifiers. Roster updates are handled for join/leave/disconnect/reconnect control events.
Notes: roster IDs are kept internally; normal output contains only display names and role/state.
Blockers: none for display behavior; lifecycle validation remains tracked under reconnect goals.

### G11 Complete `/info`

Status: DONE

Acceptance criteria:
- [x] Private/group room type, host, participant count/capacity, transport, encryption, protocol, duration, and local history state are accurate.
- [x] No codes, keys, or resume secrets are displayed.
- [x] UI/service tests cover supported room types.

Tests: `test_session_info_contains_runtime_facts_without_room_secrets`.
Notes: session duration is monotonic time since chat entry. Room code rotation does not reset room-session duration.
Blockers: none identified.

### G12 `/newcode` integration

Status: DONE

Acceptance criteria:
- [x] Host-only group command rotates code, invalidates old code server-side, resets five-minute expiry/countdown, and preserves active sessions.
- [x] A new peer can join with the new code; the old code is rejected.
- [x] Command-level integration tests cover expiry and rotation behavior.

Tests: `test_room_rotation_and_participant_cleanup`, `test_room_expiry_authentication_approval_and_capacity` exercise the command's shared rotation operation, new-code join, old-code rejection, active participant preservation, and expiry.
Notes: `/newcode` rotates the code in the running accept loop; it does not stop active sessions or restart the listener.
Blockers: none identified.

### G13 Stronger room-code authentication

Status: DONE

Acceptance criteria:
- [x] Authentication removes or defensibly reduces offline guessing without custom cryptography.
- [x] Wrong code, transcript tampering, replay, and key separation are tested.
- [x] SECURITY.md documents the exact threat model and residual risks.

Tests: `test_wire_handshake_uses_the_code_and_derives_matching_directional_keys`, `test_room_pake_rejects_tampered_key_confirmation`, `test_replayed_room_pake_transcript_is_rejected`, `test_encryption_tamper_and_replay_rejected`.
Notes: uses the maintained pure-Python `spake2` package (SPAKE2) with transcript-bound mutual confirmation, then HKDF directional keys. This removes passive offline guessing; active online guessing is rate-limited. The dependency warns that its implementation is not constant-time, and BlueChat's integration has not been independently audited.
Blockers: no software blocker identified; security review remains a release risk, not a reason to mislabel the code path.

### G14 Security regression tests

Status: DONE

Acceptance criteria:
- [x] Tests cover tampering, replay, resume misuse, forged participant IDs, oversized/malformed transfer records, checksum failure, and path traversal.
- [x] Tests assert rejected/incomplete transfers leave no published file.

Tests: `test_encryption_tamper_and_replay_rejected`, `test_resume_handshake_derives_fresh_keys_and_rejects_replay`, `test_security_regressions_reject_resume_context_and_bad_file_metadata`, `test_receive_file_malformed_offer_writes_nothing`.
Notes: coverage includes forged participant context, invalid filenames, oversized frames, checksum failure cleanup, and tampered/replayed secure packets.
Blockers: none for the listed regression criteria.

### G15 Linux native lifecycle tests

Status: DONE

Acceptance criteria:
- [x] Mocked BlueZ tests cover backend selection, state, GATT registration, advertisement start/stop, write/notify, disconnect, errors, and cleanup.
- [x] No Bluetooth hardware is required.

Tests: `test_bluez_native_server_start_advertise_and_cleanup`, `test_bluez_adapter_detection_and_powered_state_use_dbus`, `test_bluez_gatt_rx_tx_and_disconnect_callbacks`.
Notes: `test_bluez_host_start_reports_missing_gatt_capability_and_cleans_bus` covers capability errors and cleanup. Tests use mocked D-Bus and direct exported characteristic callbacks; physical tests remain separate.
Blockers: none identified.

### G16 Windows native lifecycle tests

Status: DONE

Acceptance criteria:
- [x] Mocked WinRT tests cover provider/service/characteristic creation, advertising lifecycle, writes, notifications, disconnect, errors, and cleanup.
- [x] No Bluetooth hardware is required.

Tests: `test_winrt_provider_write_notify_disconnect_and_cleanup`, `test_windows_advertising_callback_and_shutdown_cleanup`, `test_windows_peripheral_subscriber_disconnect_lifecycle`.
Notes: `test_windows_lifecycle.py` installs mocked WinRT projections and executes provider/characteristic lifecycle without Windows hardware, including invalid-offset rejection; physical tests remain separate.
Blockers: none identified.

### G17 macOS native lifecycle tests

Status: DONE

Acceptance criteria:
- [x] Mocked CoreBluetooth tests cover manager state/permission, service registration, advertising lifecycle, writes/notifications, disconnect, errors, and cleanup.
- [x] No Bluetooth hardware is required.

Tests: `test_corebluetooth_host_start_advertise_write_notify_disconnect_and_cleanup`, `test_corebluetooth_unauthorized_state_is_reported_and_cleaned`.
Notes: mocks exercise native manager state, local service, advertisement callbacks, characteristic I/O, disconnect, and cleanup; physical tests remain separate.
Blockers: none identified.

### G18 Cross-platform CI validation

Status: DONE

Acceptance criteria:
- [x] CI runs install, Ruff, mypy, tests, package build, and CLI smoke commands on Linux, Windows, and macOS.
- [x] Workflow configuration is valid and all locally available checks pass.

Tests: `.github/workflows/ci.yml` has an Ubuntu/Windows/macOS × Python 3.10/3.12 matrix. Workflow-equivalent Ruff, mypy, pytest (60 tests), build, and installed CLI smoke checks passed locally on Intel macOS.
Notes: no `.git` repository or remote is present in this workspace, so hosted CI jobs could not be triggered; Windows/Linux hosted runner results remain pending and are not claimed as passed.
Blockers: none for CI configuration; remote execution requires a repository remote/CI service.

### G19 Type-check validation

Status: DONE

Acceptance criteria:
- [x] Configured mypy command runs successfully against package code.
- [x] Meaningful issues are fixed; ignores are targeted and justified.

Tests: `python3 -m mypy src/bluechat` — success, 42 source files.
Notes: mypy is configured in `pyproject.toml` and CI; native API wrappers use targeted missing-stub overrides.
Blockers: none locally.

### G20 Clean dependency-resolving installation

Status: DONE

Acceptance criteria:
- [x] In a fresh environment, install the built wheel with dependencies resolved normally.
- [x] Verify `bluechat --version`, `bluechat --help`, and `bluechat doctor`.
- [x] Record platform-specific marker behavior where environments exist.

Tests: isolated `/private/tmp/bluechat-wheel-env`; standard dependency-resolving wheel installation followed by version/help/doctor smoke commands.
Notes: validated on Intel macOS/Python 3.11. Darwin x86_64 caps cryptography below 49 for available Intel wheels; other platforms are not executed locally.
Blockers: none for the available host platform.

### G21 Wheel installation test

Status: DONE

Acceptance criteria:
- [x] Install the built wheel into a clean environment with dependencies resolved.
- [x] Run CLI smoke commands from that installed environment.

Tests: `/private/tmp/bluechat-terminal-wheel`; the `bluechat-terminal` wheel was installed with dependency resolution; `bluechat --version`, `--help`, and `doctor` all exited successfully.
Notes: normal dependency resolution enabled.
Blockers: none on Intel macOS.

### G22 Source distribution installation test

Status: DONE

Acceptance criteria:
- [x] Install the built sdist into a clean environment with dependencies resolved.
- [x] Run CLI smoke commands from that installed environment.

Tests: `/private/tmp/bluechat-terminal-sdist`; the `bluechat-terminal` sdist produced a wheel and dependencies resolved normally, followed by version/help/doctor CLI smoke commands.
Notes: isolated Hatchling build requirements and runtime project dependencies were resolved normally.
Blockers: none on Intel macOS.

### G23 Documentation sync

Status: DONE

Acceptance criteria:
- [x] README, SECURITY.md, changelog, and hardware testing guide accurately describe supported behavior and limitations.
- [x] Group transfer, reconnection, group-host history, PAKE, CI/type-check, and package-install statuses match actual results.

Tests: manual consistency review after latest tests/build/install; all nine hardware rows remain `NOT_RUN`.
Notes: README and SECURITY identify live participant resume and the remaining host GATT service recovery limitation; CI and local platform validation are distinguished.
Blockers: none for documentation as of this cycle.

### G24 Final release checklist

Status: DONE

Acceptance criteria:
- [x] All implementable software goals are DONE.
- [x] Full tests, Ruff, mypy, wheel/sdist builds, package install smoke, and locally available CI-equivalent checks pass.
- [x] Version is appropriate; do not call stable 1.0.0 while required security/hardware criteria are pending.

Tests: `python3 -m pytest -q` — 60 passed, including `test_complete_group_transfer_reconnect_rotation_history_workflow`; `python3 -m ruff check .` — passed; `python3 -m mypy src/bluechat` — passed (42 package files); `python3 -m build --no-isolation` and `python3 -m twine check` — passed; clean `bluechat-terminal` wheel and sdist environments resolved dependencies and passed `bluechat --version`, `bluechat --help`, and `bluechat doctor`.
Notes: `0.1.0` is an unpublished TestPyPI candidate named `bluechat-terminal` because the unrelated `bluechat` name is already taken on TestPyPI. PyPI's `bluechat` name was unclaimed when checked on 2026-10-01. Hosted cross-OS CI and nine physical Bluetooth combinations remain explicitly pending; this is not a stable V1 release recommendation.
Blockers: TestPyPI and GitHub publication need account credentials; hosted CI execution and real Bluetooth hardware validation remain external release validation, not unfinished software goals.
