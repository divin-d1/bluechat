# Security policy

Please report security vulnerabilities privately to the project maintainers
before making them public. Do not include real room codes, private messages, or
personal files in reports.

BlueChat 0.1 is an educational, pre-release project. It is not audited and the
BLE backends have not been validated between physical computers. Do not rely on
this release for sensitive communication.

## Current security model and limits

- The room-code exchange uses SPAKE2 through the `spake2` package, with
  BlueChat transcript-bound mutual key confirmation, HKDF-SHA256 directional
  keys, and AES-256-GCM records. SPAKE2 is a password-authenticated key
  exchange; a passive transcript does not provide an offline code verifier.
- The six-character code still permits online guesses. The host limits
  failures and applies a cooldown after five incorrect attempts. The upstream
  pure-Python SPAKE2 implementation is not constant-time; local timing
  observation may leak information. BlueChat's integration and protocol have
  not received an independent security audit.
- Hosting pauses new authentication attempts for 30 seconds after five failed
  codes. This mitigates online guessing only and does not change offline risk.
- Each Bluetooth connection has a separate directional encryption channel.
  In group chat, the host routes messages and can read their plaintext. Group
  chat is therefore not end-to-end encrypted between clients.
- Short-lived resume credentials are room-, participant-, and session-bound,
  activated only after disconnect, single-use, and used with a fresh X25519
  exchange and transcript MAC. Private peers and group participants can resume
  during the 30-second window. Native host-service failure signals trigger a
  best-effort GATT server/advertisement restart while retaining the in-memory
  room. This recovery has automated mock coverage but is not radio-validated.
- File offers require local recipient approval. Transfers stream to a temporary
  file and verify SHA-256 before publication. Received files are never executed
  or opened automatically.
- History is local JSONL and follows the local user's preference. Do not put
  secrets in messages if history is enabled.
- BlueChat does not protect an endpoint compromised by malware or physical
  access, and it does not conceal Bluetooth radio metadata.

The room-code handshake uses the `spake2` 0.9 package, which is a pure-Python
SPAKE2 implementation and supports the project's Python baseline. It is not
the OPAQUE construction and does not eliminate active online guessing. The
upstream project warns that its arithmetic is not constant-time. BlueChat
does not implement custom cryptographic primitives; the PAKE dependency and
application protocol still need independent review before a stable release.
