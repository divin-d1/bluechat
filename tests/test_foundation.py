from __future__ import annotations

from pathlib import Path

import pytest

from bluechat.app import BlueChat
from bluechat.bluetooth.base import BluetoothDevice
from bluechat.bluetooth.fake import FakeBluetoothBackend
from bluechat.bluetooth.gatt import FragmentReassembler, fragment_packet
from bluechat.bluetooth.linux import (
    BlueChatAdvertisement,
    BlueChatGattService,
    BlueChatObjectManager,
    BlueChatRxCharacteristic,
    BlueChatTxCharacteristic,
)
from bluechat.bluetooth.macos import MacOSBluetoothBackend
from bluechat.bluetooth.macos_peripheral import CoreBluetoothConnection
from bluechat.bluetooth.windows import WindowsBluetoothBackend
from bluechat.bluetooth.windows_peripheral import WindowsGattConnection
from bluechat.chat.room import Room
from bluechat.chat.group import GroupRouter
from bluechat.chat.session import AsyncChatSession
from bluechat.config.manager import ConfigManager
from bluechat.config.models import AppConfig
from bluechat.errors import (
    ApprovalRejectedError,
    AuthenticationError,
    AuthenticationRateLimitedError,
    BluetoothUnavailableError,
    BluetoothPermissionError,
    ProtocolError,
    RoomExpiredError,
    RoomFullError,
    SecurityError,
    TransferError,
)
from bluechat.protocol.codec import decode_message, encode_message
from bluechat.protocol.framing import FrameDecoder, encode_frame
from bluechat.protocol.messages import Message
from bluechat.security.encryption import SecureChannel
from bluechat.security.handshake import (
    establish_test_pair,
    generate_room_code,
    perform_client_handshake,
    perform_host_handshake,
    validate_room_code,
)
from bluechat.security.resume import ResumeRegistry
from bluechat.security.resumption import client_resume, host_resume
from bluechat.ui.countdown import format_countdown
from bluechat.utils.validation import validate_username
from bluechat.transfer.files import (
    file_category,
    receive_stream,
    safe_filename,
    stream_file,
    unique_destination,
)
from bluechat.transfer.protocol import FileTransferManager
from bluechat.transfer.group import GroupFileRelay
from bluechat.history.manager import HistoryManager
from bluechat.config.models import HistoryPreference


def test_room_code_generation_and_validation() -> None:
    for _ in range(20):
        code = generate_room_code()
        assert len(code) == 6
        assert set(code) <= set("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
        assert validate_room_code(code.lower()) == code
    for invalid in ("", "ABC", "OOOOOO", "12345!", None):
        with pytest.raises(AuthenticationError):
            validate_room_code(invalid)  # type: ignore[arg-type]


def test_room_expiry_authentication_approval_and_capacity() -> None:
    room = Room("Host", group=True, created_at=100)
    assert room.remaining_seconds(now=100) == 300
    assert room.remaining_seconds(now=399.5) == 1
    assert room.remaining_seconds(now=400) == 0
    room.validate_code(room.code, now=399.99)
    with pytest.raises(RoomExpiredError):
        room.validate_code(room.code, now=400)
    wrong_code = "A" * 6
    while wrong_code == room.code:
        wrong_code = "B" * 6
    with pytest.raises(AuthenticationError):
        room.validate_code(wrong_code, now=101)
    room = Room("Host", group=False)
    with pytest.raises(ApprovalRejectedError):
        room.request_join("Guest", room.code, approved=False)
    room.request_join("Guest", room.code, approved=True)
    with pytest.raises(RoomFullError):
        room.request_join("Third", room.code, approved=True)
    room = Room("Host", group=True)
    for name in ("A", "B", "C", "D"):
        room.request_join(name, room.code, approved=True)
    with pytest.raises(RoomFullError):
        room.request_join("Six", room.code, approved=True)


def test_room_rotation_and_participant_cleanup() -> None:
    from bluechat.cli import _rotate_join_code

    room = Room("Host", group=True)
    old_code = room.code
    participant = room.request_join("Alex", old_code, approved=True)
    started_at = room.session_started_at
    new_code = _rotate_join_code(room)
    assert new_code != old_code
    with pytest.raises(AuthenticationError):
        room.validate_code(old_code)
    room.validate_code(new_code)
    room.request_join("Grace", new_code, approved=True)
    assert len(room.participants) == 3
    assert room.session_started_at == started_at
    assert room.remove_participant(participant)
    assert not room.remove_participant(participant)
    assert room.list_participants()[0] == ("host", "Host")
    assert room.list_participants()[1][1] == "Grace"


def test_session_info_contains_runtime_facts_without_room_secrets(monkeypatch) -> None:
    from bluechat.cli import _format_session_info, _format_who

    monkeypatch.setattr("bluechat.cli.time.monotonic", lambda: 7_385)
    value = _format_session_info("Group", "Divin", 4, 6_500, True)
    assert "Room type: Group" in value
    assert "Host: Divin" in value
    assert "Participants: 4/5" in value
    assert "Session duration: 00:14:45" in value
    assert "History: Enabled locally" in value
    assert "room code" not in value.lower()
    roster = _format_who(
        {"host": "Divin", "participant-a": "Alex", "participant-g": "Grace"},
        reconnecting={"participant-g"},
    )
    assert roster == "Participants\nDivin (Host)\nAlex\nGrace — reconnecting"
    assert "participant-g" not in roster


def test_room_authentication_attempts_are_rate_limited() -> None:
    room = Room("Host", group=False)
    for _ in range(4):
        room.record_auth_failure(now=100)
    room.check_auth_rate_limit(now=100)
    room.record_auth_failure(now=100)
    with pytest.raises(AuthenticationRateLimitedError):
        room.check_auth_rate_limit(now=129.9)
    room.check_auth_rate_limit(now=130)
    room.record_auth_success()
    assert room.auth_failures == 0


def test_resume_credentials_are_bound_single_use_and_expiring() -> None:
    now = [100.0]
    registry = ResumeRegistry(clock=lambda: now[0])
    token = registry.issue("room", "participant")
    assert not registry.consume(token, "other-room", "participant")
    assert not registry.consume(token, "room", "participant")
    token = registry.issue("room", "participant")
    assert registry.consume(token, "room", "participant")
    assert not registry.consume(token, "room", "participant")
    expired = registry.issue("room", "participant")
    now[0] += 31
    assert not registry.consume(expired, "room", "participant")
    assert registry.purge() == 0


def test_live_peer_resume_token_activates_only_on_disconnect() -> None:
    now = [100.0]
    registry = ResumeRegistry(clock=lambda: now[0])
    token = registry.issue("room", "participant", "session", deferred_expiry=True)
    assert not registry.verify(token, "room", "participant", "session")
    assert not registry.suspend(token, "room", "other", "session")
    assert registry.suspend(token, "room", "participant", "session")
    assert registry.verify(token, "room", "participant", "session")
    assert not registry.verify(token, "room", "participant", "other-session")
    now[0] += 30
    assert not registry.verify(token, "room", "participant", "session")


def test_resume_handshake_derives_fresh_keys_and_rejects_replay() -> None:
    import asyncio

    class QueueConnection:
        def __init__(self) -> None:
            self.incoming = asyncio.Queue()
            self.peer = None

        async def send(self, data: bytes) -> None:
            await self.peer.incoming.put(data)

        async def receive(self) -> bytes:
            return await self.incoming.get()

    async def run() -> None:
        registry = ResumeRegistry()
        session_id, room_id, participant_id = "session-A", "room-A", "participant-A"
        token = registry.issue(room_id, participant_id, session_id)
        host_conn, client_conn = QueueConnection(), QueueConnection()
        host_conn.peer, client_conn.peer = client_conn, host_conn
        host_task = asyncio.create_task(
            host_resume(host_conn, registry, session_id, lambda _room, _participant: token)
        )
        client_keys = await client_resume(client_conn, token, room_id, session_id, participant_id)
        host_keys, bound_room, bound_participant = await host_task
        assert (bound_room, bound_participant) == (room_id, participant_id)
        assert client_keys.send_key == host_keys.receive_key
        assert client_keys.receive_key == host_keys.send_key
        assert client_keys.transcript_id == host_keys.transcript_id
        assert client_keys.send_key != establish_test_pair("M7KP4X")[0].send_key
        assert not registry.verify(token, room_id, participant_id, session_id)

        replay_host, replay_client = QueueConnection(), QueueConnection()
        replay_host.peer, replay_client.peer = replay_client, replay_host
        replay = asyncio.create_task(
            host_resume(replay_host, registry, session_id, lambda _r, _p: token)
        )
        with pytest.raises(AuthenticationError):
            await client_resume(replay_client, token, room_id, session_id, participant_id)
        with pytest.raises(AuthenticationError):
            await replay

    asyncio.run(run())


def test_username_rejects_empty_control_and_long_values() -> None:
    assert validate_username("  Divin ") == "Divin"
    for value in ("", "  ", "x\nadmin", "a" * 33, "hello\u200b"):
        with pytest.raises(ValueError):
            validate_username(value)


def test_countdown_format_is_safe_for_unexpected_values() -> None:
    assert format_countdown(300) == "05:00"
    assert format_countdown(1) == "00:01"
    assert format_countdown(-5) == "00:00"


def test_message_round_trip_and_unexpected_inputs() -> None:
    msg = Message("TEXT_MESSAGE", {"text": "Hello"}, message_id="id")
    assert decode_message(encode_message(msg)) == msg
    for raw in (
        b"not json",
        b'{"version":2,"type":"PING","payload":{}}',
        b'{"version":1,"type":"UNKNOWN","payload":{}}',
        b'{"version":1,"type":"PING","payload":{},"extra":1}',
        b'{"version":1,"type":"PING","payload":{"n":NaN}}',
    ):
        with pytest.raises(ProtocolError):
            decode_message(raw)


def test_framing_partial_multiple_and_invalid_lengths() -> None:
    first, second = encode_frame(b"first"), encode_frame(b"second")
    decoder = FrameDecoder()
    assert decoder.feed(first[:2]) == []
    assert decoder.feed(first[2:] + second) == [b"first", b"second"]
    decoder = FrameDecoder()
    with pytest.raises(ProtocolError):
        decoder.feed(b"\xff\xff\xff\xff")
    with pytest.raises(ProtocolError):
        decoder.feed(encode_frame(b"later"))


def test_encryption_tamper_and_replay_rejected() -> None:
    left_keys, right_keys = establish_test_pair("M7KP4X")
    left = SecureChannel(left_keys.send_key, left_keys.receive_key)
    right = SecureChannel(right_keys.send_key, right_keys.receive_key)
    packet = left.encrypt(b"secret")
    changed = packet[:-1] + bytes([packet[-1] ^ 1])
    with pytest.raises(SecurityError):
        right.decrypt(changed)
    assert right.decrypt(packet) == b"secret"
    with pytest.raises(SecurityError):
        right.decrypt(packet)


def test_wrong_room_code_fails_authentication() -> None:
    with pytest.raises(AuthenticationError):
        establish_test_pair("M7KP4X", "R8FN2Q")


def test_config_round_trip_and_corruption(tmp_path) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    config = AppConfig(username="Divin", download_dir=tmp_path / "BlueChat")
    manager.save(config)
    loaded = manager.load()
    assert loaded.username == "Divin"
    assert loaded.download_dir == tmp_path / "BlueChat"
    manager.path.write_text("username = [bad", encoding="utf-8")
    recovered = manager.load()
    assert recovered.username is None
    assert manager.path.with_suffix(".toml.corrupt").exists()


def test_public_api_room_to_encrypted_memory_messages(tmp_path) -> None:
    app = BlueChat("Host", config_manager=ConfigManager(tmp_path / "cfg.toml"))
    room = app.create_room()
    _, host, guest = app.join_memory_room(room, room.code, username="Guest", approved=True)
    try:
        guest.send_text("hello")
        assert host.receive(timeout=1).text == "hello"
        host.send_text("world")
        assert guest.receive(timeout=1).text == "world"
        with pytest.raises(ValueError):
            guest.send_text(" " * 2)
    finally:
        host.close()
        guest.close()


def test_fake_bluetooth_backend_is_injectable_and_copies_scan_results() -> None:
    backend = FakeBluetoothBackend([BluetoothDevice("Laptop", paired=False)])
    import asyncio

    async def run() -> None:
        found = await backend.discover()
        assert found == [BluetoothDevice("Laptop", paired=False)]
        assert await backend.pair(found[0])
        assert (await backend.discover())[0].paired
        assert found[0].paired is False

    asyncio.run(run())
    assert not asyncio.run(FakeBluetoothBackend(available=False).is_available())


def test_ble_fragments_reassemble_out_of_order_and_reject_bad_data() -> None:
    payload = bytes(range(100))
    fragments = fragment_packet(payload, fragment_size=10, frame_id=42)
    reassembler = FragmentReassembler()
    decoded = None
    for part in reversed(fragments):
        decoded = reassembler.feed(part) or decoded
    assert decoded == payload
    with pytest.raises(ProtocolError):
        reassembler.feed(b"short")


def test_wire_handshake_uses_the_code_and_derives_matching_directional_keys() -> None:
    import asyncio

    class QueueConnection:
        def __init__(self) -> None:
            self.incoming = asyncio.Queue()
            self.peer = None
            self.peer_id = "fake"

        async def send(self, data: bytes) -> None:
            await self.peer.incoming.put(data)

        async def receive(self) -> bytes:
            return await self.incoming.get()

        async def close(self) -> None:
            return None

    async def run(code: str, peer_code: str):
        host, client = QueueConnection(), QueueConnection()
        host.peer, client.peer = client, host
        return await asyncio.gather(
            perform_host_handshake(host, code),
            perform_client_handshake(client, peer_code),
            return_exceptions=True,
        )

    host_keys, client_keys = asyncio.run(run("M7KP4X", "M7KP4X"))
    assert not isinstance(host_keys, Exception)
    assert not isinstance(client_keys, Exception)
    assert host_keys.send_key == client_keys.receive_key
    assert host_keys.receive_key == client_keys.send_key
    assert host_keys.transcript_id == client_keys.transcript_id
    fresh_host, fresh_client = asyncio.run(run("M7KP4X", "M7KP4X"))
    assert fresh_host.transcript_id != host_keys.transcript_id
    assert fresh_host.send_key == fresh_client.receive_key
    host_error, client_error = asyncio.run(run("M7KP4X", "R8FN2Q"))
    assert isinstance(host_error, AuthenticationError)
    assert isinstance(client_error, AuthenticationError)


def test_room_pake_rejects_tampered_key_confirmation() -> None:
    import asyncio

    class QueueConnection:
        def __init__(self) -> None:
            self.incoming = asyncio.Queue()
            self.peer = None

        async def send(self, data: bytes) -> None:
            await self.peer.incoming.put(data)

        async def receive(self) -> bytes:
            return await self.incoming.get()

    class TamperClientProof:
        def __init__(self, connection) -> None:
            self.connection = connection

        async def send(self, data: bytes) -> None:
            await self.connection.send(data)

        async def receive(self) -> bytes:
            data = await self.connection.receive()
            if data.startswith(b"BC-PC2"):
                return data[:-1] + bytes([data[-1] ^ 1])
            return data

    async def run() -> None:
        host, client = QueueConnection(), QueueConnection()
        host.peer, client.peer = client, host
        host_result, client_result = await asyncio.gather(
            perform_host_handshake(TamperClientProof(host), "M7KP4X"),
            perform_client_handshake(client, "M7KP4X"),
            return_exceptions=True,
        )
        assert isinstance(host_result, AuthenticationError)
        assert isinstance(client_result, AuthenticationError)

    asyncio.run(run())


def test_replayed_room_pake_transcript_is_rejected() -> None:
    import asyncio

    class QueueConnection:
        def __init__(self) -> None:
            self.incoming = asyncio.Queue()
            self.peer = None

        async def send(self, data: bytes) -> None:
            await self.peer.incoming.put(data)

        async def receive(self) -> bytes:
            return await self.incoming.get()

    class CaptureClient:
        def __init__(self, connection) -> None:
            self.connection = connection
            self.sent: list[bytes] = []

        async def send(self, data: bytes) -> None:
            self.sent.append(data)
            await self.connection.send(data)

        async def receive(self) -> bytes:
            return await self.connection.receive()

    class ReplayPeer:
        def __init__(self, packets: list[bytes]) -> None:
            self.packets = iter(packets)
            self.sent: list[bytes] = []

        async def receive(self) -> bytes:
            return next(self.packets)

        async def send(self, data: bytes) -> None:
            self.sent.append(data)

    async def run() -> None:
        host_conn, client_conn = QueueConnection(), QueueConnection()
        host_conn.peer, client_conn.peer = client_conn, host_conn
        capture = CaptureClient(client_conn)
        await asyncio.gather(
            perform_host_handshake(host_conn, "M7KP4X"),
            perform_client_handshake(capture, "M7KP4X"),
        )
        replay = ReplayPeer(capture.sent)
        with pytest.raises(AuthenticationError):
            await perform_host_handshake(replay, "M7KP4X")
        assert replay.sent[0].startswith(b"BC-PB2")
        assert replay.sent[-1] == b"BC-ER1"

    asyncio.run(run())


def test_async_protocol_session_sends_and_receives_encrypted_messages() -> None:
    import asyncio

    class QueueConnection:
        def __init__(self, peer_id: str) -> None:
            self.peer_id = peer_id
            self.incoming = asyncio.Queue()
            self.peer = None
            self.closed = False

        async def send(self, data: bytes) -> None:
            await self.peer.incoming.put(data)

        async def receive(self) -> bytes:
            value = await self.incoming.get()
            if value is None:
                raise ConnectionError
            return value

        async def close(self) -> None:
            self.closed = True
            await self.peer.incoming.put(None)

    async def run() -> None:
        host_connection, guest_connection = QueueConnection("host"), QueueConnection("guest")
        host_connection.peer, guest_connection.peer = guest_connection, host_connection
        host_keys, guest_keys = establish_test_pair("M7KP4X")
        host = AsyncChatSession("Host", None, host_connection, host_keys)
        guest = AsyncChatSession("Guest", "Host", guest_connection, guest_keys)
        host.start()
        guest.start()
        await guest.send_control("HELLO", {"username": "Guest"})
        hello = await host.receive_control("HELLO", timeout=1)
        assert hello.payload["username"] == "Guest"
        host.peer_username = "Guest"
        await guest.send_text("Hello host")
        assert (await host.receive()).text == "Hello host"
        await host.send_text("Hello guest")
        assert (await guest.receive()).text == "Hello guest"
        await host.close()
        await guest.close()

    asyncio.run(run())


def test_group_router_routes_once_without_echoing_to_sender() -> None:
    import asyncio

    class QueueConnection:
        def __init__(self) -> None:
            self.incoming = asyncio.Queue()
            self.peer = None

        async def send(self, data: bytes) -> None:
            await self.peer.incoming.put(data)

        async def receive(self) -> bytes:
            packet = await self.incoming.get()
            if packet is None:
                raise ConnectionError("closed")
            return packet

        async def close(self) -> None:
            await self.peer.incoming.put(None)

    async def run() -> None:
        room_id = "room-test"
        router = GroupRouter(room_id)
        sessions = []
        for name, participant_id in (("Alex", "a"), ("Grace", "g")):
            host_connection, client_connection = QueueConnection(), QueueConnection()
            host_connection.peer, client_connection.peer = client_connection, host_connection
            host_keys, client_keys = establish_test_pair("M7KP4X")
            host_session = AsyncChatSession(
                "Host",
                name,
                host_connection,
                host_keys,
                participant_id=f"host-{participant_id}",
                room_id=room_id,
            )
            guest_session = AsyncChatSession(
                name,
                "Host",
                client_connection,
                client_keys,
                participant_id=participant_id,
                room_id=room_id,
                allow_remote_senders=True,
            )
            host_session.start()
            guest_session.start()
            await router.add(participant_id, name, host_session)
            sessions.append((host_session, guest_session))

        await sessions[0][1].send_text("hello group")
        received = await asyncio.wait_for(sessions[1][1].receive(), timeout=1)
        assert received.sender == "Alex"
        assert received.sender_id == "a"
        assert received.room_id == room_id
        assert received.text == "hello group"
        assert router.incoming.qsize() == 1
        assert await router.remove("g")
        await router.close()
        for _, guest in sessions:
            await guest.close()

    asyncio.run(run())


def test_file_safety_streaming_checksum_and_categories(tmp_path) -> None:
    import hashlib

    assert safe_filename("../../photo.jpg") == "photo.jpg"
    assert safe_filename("C:\\Windows\\CON.txt").startswith("_CON")
    with pytest.raises(TransferError):
        safe_filename("../")
    assert file_category("clip.mp4") == "Videos"
    assert file_category("photo.webp") == "Images"
    source = tmp_path / "large image.png"
    contents = b"0123456789" * 10_000
    source.write_bytes(contents)
    size, digest, chunks = stream_file(source, chunk_size=1024)
    output_dir = tmp_path / "downloads" / "Images"
    output_dir.mkdir(parents=True)
    destination = unique_destination(output_dir, source.name)
    saved = receive_stream(chunks, destination, expected_size=size, expected_sha256=digest)
    assert saved.read_bytes() == contents
    assert digest == hashlib.sha256(contents).hexdigest()


def test_security_regressions_reject_resume_context_and_bad_file_metadata(tmp_path) -> None:
    import asyncio
    import hashlib

    async def run_resume() -> None:
        class QueueConnection:
            def __init__(self) -> None:
                self.incoming = asyncio.Queue()
                self.peer = None

            async def send(self, data: bytes) -> None:
                await self.peer.incoming.put(data)

            async def receive(self) -> bytes:
                return await self.incoming.get()

        registry = ResumeRegistry()
        token = registry.issue("room-1", "participant-1", "session-1")
        host, client = QueueConnection(), QueueConnection()
        host.peer, client.peer = client, host
        host_task = asyncio.create_task(
            host_resume(host, registry, "session-1", lambda _room, _pid: token)
        )
        with pytest.raises(AuthenticationError):
            await client_resume(client, token, "room-1", "session-1", "forged-participant")
        with pytest.raises(AuthenticationError):
            await host_task
        assert registry.verify(token, "room-1", "participant-1", "session-1")
        assert not registry.verify(token, "room-1", "forged-participant", "session-1")

    asyncio.run(run_resume())

    # Direct streaming helper must leave no published file after checksum failure.
    destination = tmp_path / "downloads"
    with pytest.raises(TransferError, match="integrity"):
        receive_stream(
            iter([b"tampered"]),
            destination,
            expected_size=len(b"tampered"),
            expected_sha256=hashlib.sha256(b"expected").hexdigest(),
        )
    assert not (destination / "corrupt.bin").exists()
    assert list(tmp_path.rglob("*.part")) == []

    for filename in ("../../escape.txt", "/etc/passwd", "C:\\Windows\\file.txt"):
        assert Path(safe_filename(filename)).name == safe_filename(filename)
    for invalid_filename in ("../", "..\\", "\x00bad"):
        with pytest.raises(TransferError):
            safe_filename(invalid_filename)

    # The codec rejects malformed/oversized network records before dispatch.
    decoder = FrameDecoder(max_frame_size=32)
    with pytest.raises(ProtocolError):
        decoder.feed((33).to_bytes(4, "big") + b"x" * 33)


def test_receive_file_malformed_offer_writes_nothing(tmp_path) -> None:
    import asyncio
    from types import SimpleNamespace

    async def run() -> None:
        manager = FileTransferManager()

        async def approve(*_args):
            return tmp_path

        malformed = SimpleNamespace(
            payload={
                "transfer_id": "x",
                "filename": "../../escape.bin",
                "size": 4,
                "sha256": "invalid",
                "mime_type": "application/octet-stream",
            }
        )

        class Session:
            def open_transfer(self, _transfer_id):
                raise AssertionError("malformed offer must be rejected before transfer setup")

        with pytest.raises(TransferError):
            await manager.receive_file(Session(), malformed, approve)

    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []


def test_history_is_local_preference_controlled_and_sanitized(tmp_path) -> None:
    history = HistoryManager(tmp_path / "history")
    assert history.create("Alex", room_id="room-123456", preference=HistoryPreference.NEVER) is None
    assert history.create("Alex", room_id="room-123456", preference=HistoryPreference.ASK) is None
    assert (
        history.create("Alex", room_id="room-123456", preference=HistoryPreference.ALWAYS)
        is not None
    )
    path = history.create(
        "../../Alex", room_id="room-123456", preference=HistoryPreference.ASK, consent=True
    )
    assert path is not None and path.parent == tmp_path / "history"
    history.append(path, "message", sender="Alex", text="hello", metadata={"kind": "private"})
    history.append(path, "joined", sender="Grace")
    history.append(path, "left", sender="Bob")
    history.append(path, "file", sender="Alex", metadata={"filename": "photo.png", "size": 24})
    assert '"text":"hello"' in path.read_text(encoding="utf-8")
    stored = path.read_text(encoding="utf-8")
    assert '"event":"joined"' in stored
    assert '"event":"left"' in stored
    assert '"filename":"photo.png"' in stored
    # Disk/permission errors are logged and must not escape into room handling.
    history.append(tmp_path / "missing" / "history.jsonl", "message", text="not fatal")


def test_async_file_transfer_requires_approval_and_verifies_digest(tmp_path) -> None:
    import asyncio

    class QueueConnection:
        def __init__(self) -> None:
            self.incoming = asyncio.Queue()
            self.peer = None

        async def send(self, data: bytes) -> None:
            await self.peer.incoming.put(data)

        async def receive(self) -> bytes:
            packet = await self.incoming.get()
            if packet is None:
                raise ConnectionError("closed")
            return packet

        async def close(self) -> None:
            await self.peer.incoming.put(None)

    async def run() -> None:
        host_conn, client_conn = QueueConnection(), QueueConnection()
        host_conn.peer, client_conn.peer = client_conn, host_conn
        host_keys, client_keys = establish_test_pair("M7KP4X")
        host = AsyncChatSession("Host", "Guest", host_conn, host_keys)
        guest = AsyncChatSession("Guest", "Host", client_conn, client_keys)
        host.start()
        guest.start()
        source = tmp_path / "file with spaces.pdf"
        content = b"bluechat" * 18_000
        source.write_bytes(content)
        manager = FileTransferManager(chunk_size=2048)

        async def approve(filename: str, size: int, mime: str):
            assert filename == source.name
            assert size == len(content)
            assert mime == "application/pdf"
            return tmp_path / "received"

        send_task = asyncio.create_task(manager.send_file(host, source))
        offer = await guest.receive_control("FILE_OFFER", timeout=2)
        output = await manager.receive_file(guest, offer, approve)
        assert output is not None
        assert output.read_bytes() == content
        await send_task
        await host.close()
        await guest.close()

    asyncio.run(run())


def test_group_file_relay_has_independent_approval_and_integrity(tmp_path) -> None:
    import asyncio

    class QueueConnection:
        def __init__(self) -> None:
            self.incoming = asyncio.Queue()
            self.peer = None

        async def send(self, data: bytes) -> None:
            await self.peer.incoming.put(data)

        async def receive(self) -> bytes:
            packet = await self.incoming.get()
            if packet is None:
                raise ConnectionError("closed")
            return packet

        async def close(self) -> None:
            await self.peer.incoming.put(None)

    async def pair(name: str, participant_id: str, room_id: str):
        host_conn, client_conn = QueueConnection(), QueueConnection()
        host_conn.peer, client_conn.peer = client_conn, host_conn
        host_keys, client_keys = establish_test_pair("M7KP4X")
        host = AsyncChatSession(
            "Host",
            name,
            host_conn,
            host_keys,
            participant_id=f"host-{participant_id}",
            room_id=room_id,
        )
        client = AsyncChatSession(
            name,
            "Host",
            client_conn,
            client_keys,
            participant_id=participant_id,
            room_id=room_id,
            allow_remote_senders=True,
        )
        host.start()
        client.start()
        return host, client

    async def run() -> None:
        room_id = "group-transfer-test"
        router = GroupRouter(room_id)
        relay = GroupFileRelay(router, peer_timeout=2)
        router.on_control = relay.handle
        host_sender, sender = await pair("Alex", "alex", room_id)
        host_accept, accepter = await pair("Grace", "grace", room_id)
        host_reject, rejecter = await pair("Bob", "bob", room_id)
        host_slow, slow_peer = await pair("Slow", "slow", room_id)
        await router.add("alex", "Alex", host_sender)
        await router.add("grace", "Grace", host_accept)
        await router.add("bob", "Bob", host_reject)
        await router.add("slow", "Slow", host_slow)

        source = tmp_path / "large group image.png"
        payload = b"image-data" * 25_000
        source.write_bytes(payload)
        manager = FileTransferManager(chunk_size=2048)

        async def accept_file():
            offer = await accepter.receive_control("FILE_OFFER", timeout=3)
            # Ordinary chat continues over the independent group-message path.
            await accepter.send_text("still responsive during transfer")

            async def approve(_name, _size, _mime):
                return tmp_path / "accepted"

            return await manager.receive_file(accepter, offer, approve)

        async def reject_file():
            offer = await rejecter.receive_control("FILE_OFFER", timeout=3)

            async def decline(_name, _size, _mime):
                return None

            return await manager.receive_file(rejecter, offer, decline)

        async def accept_but_never_ack():
            offer = await slow_peer.receive_control("FILE_OFFER", timeout=3)
            slow_peer.open_transfer(offer.payload["transfer_id"])
            await slow_peer.send_control(
                "FILE_ACCEPT", {"transfer_id": offer.payload["transfer_id"]}
            )

        accepted_task = asyncio.create_task(accept_file())
        rejected_task = asyncio.create_task(reject_file())
        slow_task = asyncio.create_task(accept_but_never_ack())
        start = asyncio.get_running_loop().time()
        transfer_id = await asyncio.wait_for(manager.send_file(sender, source), timeout=8)
        received, rejected, _ = await asyncio.gather(accepted_task, rejected_task, slow_task)
        assert transfer_id
        assert asyncio.get_running_loop().time() - start < relay.peer_timeout + 4
        routed = await asyncio.wait_for(router.incoming.get(), timeout=1)
        assert routed.text == "still responsive during transfer"
        assert received is not None and received.read_bytes() == payload
        assert rejected is None
        # Declining peer receives the offer, but never receives a start or bytes.
        assert rejecter._controls.empty()
        await relay.close()
        await router.close()
        await asyncio.gather(sender.close(), accepter.close(), rejecter.close(), slow_peer.close())

    asyncio.run(run())


def test_bluez_gatt_service_definitions_introspect() -> None:
    pytest.importorskip("dbus_next")

    class StubServer:
        def receive_fragment(self, peer_id: str, fragment: bytes) -> None:
            return None

    interfaces = [
        BlueChatObjectManager().interface,
        BlueChatGattService().interface,
        BlueChatRxCharacteristic(StubServer()).interface,
        BlueChatTxCharacteristic().interface,
        BlueChatAdvertisement("BlueChat").interface,
    ]
    assert [interface.introspect().name for interface in interfaces] == [
        "org.freedesktop.DBus.ObjectManager",
        "org.bluez.GattService1",
        "org.bluez.GattCharacteristic1",
        "org.bluez.GattCharacteristic1",
        "org.bluez.LEAdvertisement1",
    ]


def test_macos_backend_exposes_both_central_and_peripheral_roles() -> None:
    backend = MacOSBluetoothBackend()
    capabilities = backend.capabilities
    assert capabilities.discovery
    assert capabilities.hosting
    assert capabilities.advertising
    assert capabilities.multiple_peers
    assert capabilities.max_peers == 4


def test_bluetooth_permission_failures_are_not_downgraded_to_missing_adapter() -> None:
    from bluechat.errors import is_bluetooth_permission_error

    class AccessDenied(Exception):
        pass

    assert is_bluetooth_permission_error(AccessDenied("system access denied"))
    assert not is_bluetooth_permission_error(OSError("adapter not found"))
    assert issubclass(BluetoothPermissionError, Exception)


def test_corebluetooth_connection_reassembles_and_signals_disconnect() -> None:
    import asyncio

    class StubServer:
        pass

    async def run() -> None:
        connection = CoreBluetoothConnection("peer", StubServer())  # type: ignore[arg-type]
        fragments = fragment_packet(b"hello from BLE", fragment_size=3, frame_id=9)
        for fragment in fragments:
            connection.feed_fragment(fragment)
        assert await connection.receive() == b"hello from BLE"
        connection.mark_disconnected()
        with pytest.raises(ConnectionError):
            await connection.receive()

    asyncio.run(run())


def test_corebluetooth_peripheral_subscribe_disconnect_lifecycle() -> None:
    import asyncio
    from bluechat.bluetooth.macos_peripheral import CoreBluetoothPeripheral

    class Central:
        def __init__(self, identifier: str) -> None:
            self.value = identifier

        def identifier(self):
            return self

        def UUIDString(self):
            return self.value

    async def run() -> None:
        peripheral = CoreBluetoothPeripheral()
        central = Central("central-one")
        await peripheral._on_subscribe(central)
        connection = await peripheral.accept()
        await peripheral._on_unsubscribe(central)
        with pytest.raises(ConnectionError):
            await connection.receive()
        await peripheral.stop()

    asyncio.run(run())


def test_corebluetooth_advertisement_and_shutdown_cleanup() -> None:
    import asyncio
    from bluechat.bluetooth.macos_peripheral import CoreBluetoothPeripheral

    class Peripheral:
        def __init__(self) -> None:
            self.calls = []

        def stopAdvertising(self):
            self.calls.append("stop-advertising")

        def removeAllServices(self):
            self.calls.append("remove-services")

    async def run() -> None:
        backend = CoreBluetoothPeripheral()
        native = Peripheral()
        backend._peripheral = native
        backend._dispatch_queue = object()
        backend._dispatch_async = lambda _queue, callback: callback()
        backend._advertised = asyncio.get_running_loop().create_future()
        backend._finish_advertisement(None)
        assert backend._advertised.done()
        await backend.stop_advertising()
        assert native.calls == ["stop-advertising"]
        await backend.stop()
        assert native.calls == ["stop-advertising", "stop-advertising", "remove-services"]
        assert backend._peripheral is None

    asyncio.run(run())


def test_bluez_peer_connection_reassembles_and_cleans_up() -> None:
    import asyncio
    from bluechat.bluetooth.linux import BlueZPeerConnection

    class FakeTx:
        async def notify_value(self, value: bytes) -> None:
            self.sent.append(value)

        def __init__(self) -> None:
            self.sent = []

    async def run() -> None:
        tx = FakeTx()
        connection = BlueZPeerConnection("peer", tx)  # type: ignore[arg-type]
        for fragment in fragment_packet(b"linux lifecycle", fragment_size=4, frame_id=11):
            connection.feed_fragment(fragment)
        assert await connection.receive() == b"linux lifecycle"
        await connection.close()
        with pytest.raises(ConnectionError):
            await connection.receive()

    asyncio.run(run())


def test_bluez_server_peer_admission_and_capacity() -> None:
    pytest.importorskip("dbus_next")
    import asyncio
    from bluechat.bluetooth.linux import BlueZGattServer

    async def run() -> None:
        server = BlueZGattServer(max_peers=1)
        server.receive_fragment("/peer/one", fragment_packet(b"one")[0])
        first = await server.accept()
        assert first.peer_id == "/peer/one"
        assert await first.receive() == b"one"
        server.receive_fragment("/peer/two", fragment_packet(b"two")[0])
        assert server._accepted.empty()
        await first.close()
        server._peers.pop("/peer/one", None)
        with pytest.raises(ConnectionError):
            await first.receive()

    asyncio.run(run())


def test_bluez_native_server_start_advertise_and_cleanup(monkeypatch) -> None:
    pytest.importorskip("dbus_next")
    import asyncio
    from types import SimpleNamespace
    import dbus_next.aio
    from dbus_next import MessageType
    from bluechat.bluetooth.linux import BlueZGattServer

    class Manager:
        def __init__(self) -> None:
            self.calls = []

        def __getattr__(self, name):
            async def call(*args):
                self.calls.append((name, args))

            return call

    class Bus:
        def __init__(self, **_kwargs) -> None:
            self.exported = []
            self.unexported = []
            self.managers = {
                "org.bluez.GattManager1": Manager(),
                "org.bluez.LEAdvertisingManager1": Manager(),
            }
            self.closed = False

        async def connect(self):
            return self

        async def introspect(self, _destination, path):
            return path

        def get_proxy_object(self, _destination, path, _introspection):
            if path == "/":
                return SimpleNamespace(
                    get_interface=lambda _name: SimpleNamespace(
                        call_get_managed_objects=lambda: None
                    )
                )
            return SimpleNamespace(get_interface=lambda name: self.managers[name])

        def export(self, path, interface):
            self.exported.append((path, interface))

        def unexport(self, path, interface):
            self.unexported.append((path, interface))

        def add_message_handler(self, _handler):
            return None

        async def call(self, _message):
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN)

        def disconnect(self):
            self.closed = True

    bus_instance = Bus()
    adapter_path = "/org/bluez/hci0"

    async def managed_objects():
        return {
            adapter_path: {
                "org.bluez.GattManager1": {},
                "org.bluez.LEAdvertisingManager1": {},
            }
        }

    original_proxy = Bus.get_proxy_object

    def proxy(self, destination, path, introspection):
        if path == "/":
            return SimpleNamespace(
                get_interface=lambda _name: SimpleNamespace(
                    call_get_managed_objects=managed_objects
                )
            )
        return original_proxy(self, destination, path, introspection)

    monkeypatch.setattr(Bus, "get_proxy_object", proxy)
    monkeypatch.setattr(dbus_next.aio, "MessageBus", lambda **_kwargs: bus_instance)

    async def run() -> None:
        server = BlueZGattServer()
        await server.start()
        assert server.adapter_path == adapter_path
        assert len(bus_instance.exported) == 4
        await server.advertise("BlueChat-host")
        assert len(bus_instance.exported) == 5
        await server.stop_advertising()
        await server.stop()
        assert bus_instance.closed
        assert len(bus_instance.unexported) >= 5
        assert server.bus is None

    asyncio.run(run())


def test_bluez_gatt_rx_tx_and_disconnect_callbacks() -> None:
    import asyncio
    from types import SimpleNamespace

    pytest.importorskip("dbus_next")
    from dbus_next import MessageType
    from bluechat.bluetooth.gatt import fragment_packet
    from bluechat.bluetooth.linux import BlueZGattServer

    async def run() -> None:
        server = BlueZGattServer(max_peers=1)
        server.adapter_path = "/org/bluez/hci0"
        server.tx.interface.notifying = True
        fragment = fragment_packet(b"peer-data")[0]
        server._rx.interface.WriteValue(fragment, {"device": "/org/bluez/hci0/dev_A"})
        peer = await server.accept()
        assert await peer.receive() == b"peer-data"

        await peer.send(b"host-data")
        tx_fragment = server.tx.interface.Value
        from bluechat.bluetooth.gatt import FragmentReassembler

        assert FragmentReassembler().feed(tx_fragment) == b"host-data"
        disconnected = SimpleNamespace(
            message_type=MessageType.SIGNAL,
            interface="org.freedesktop.DBus.Properties",
            member="PropertiesChanged",
            body=["org.bluez.Device1", {"Connected": False}],
            path="/org/bluez/hci0/dev_A",
        )
        server._handle_message(disconnected)
        with pytest.raises(ConnectionError):
            await peer.receive()
        powered_off = SimpleNamespace(
            message_type=MessageType.SIGNAL,
            interface="org.freedesktop.DBus.Properties",
            member="PropertiesChanged",
            body=["org.bluez.Adapter1", {"Powered": False}],
            path=server.adapter_path,
        )
        server._handle_message(powered_off)
        await asyncio.wait_for(server.wait_host_failure(), timeout=0.1)

    asyncio.run(run())


def test_bluez_host_start_reports_missing_gatt_capability_and_cleans_bus(monkeypatch) -> None:
    pytest.importorskip("dbus_next")
    import asyncio
    from types import SimpleNamespace
    import dbus_next.aio
    from dbus_next import MessageType
    from bluechat.bluetooth.linux import BlueZGattServer

    class Bus:
        def __init__(self, **_kwargs) -> None:
            self.disconnected = False

        async def connect(self):
            return self

        async def call(self, _message):
            return SimpleNamespace(message_type=MessageType.METHOD_RETURN)

        async def introspect(self, _destination, _path):
            return object()

        def get_proxy_object(self, _destination, _path, _introspection):
            async def managed_objects():
                return {}

            return SimpleNamespace(
                get_interface=lambda _name: SimpleNamespace(
                    call_get_managed_objects=managed_objects
                )
            )

        def add_message_handler(self, _handler):
            return None

        def disconnect(self):
            self.disconnected = True

    bus = Bus()
    monkeypatch.setattr(dbus_next.aio, "MessageBus", lambda **_kwargs: bus)

    async def run() -> None:
        server = BlueZGattServer()
        with pytest.raises(BluetoothUnavailableError, match="lacks GATT hosting"):
            await server.start()
        assert bus.disconnected
        assert server.bus is None

    asyncio.run(run())


def test_bluetooth_startup_probe_timeout_is_translated(monkeypatch) -> None:
    import asyncio
    import bluechat.bluetooth.manager as manager_module
    from bluechat.bluetooth.manager import BluetoothManager

    class SlowBackend(FakeBluetoothBackend):
        async def is_available(self) -> bool:
            await asyncio.Event().wait()
            return True

    monkeypatch.setattr(manager_module, "BLUETOOTH_PROBE_TIMEOUT", 0.001)

    async def run() -> None:
        manager = BluetoothManager(SlowBackend())
        with pytest.raises(BluetoothUnavailableError, match="detection timed out"):
            await manager.require_ready(role="host")

    asyncio.run(run())


def test_doctor_bounds_native_adapter_probes(tmp_path: Path, monkeypatch, capsys) -> None:
    import asyncio
    import bluechat.cli as cli_module
    from bluechat.bluetooth.manager import BluetoothManager
    from bluechat.config.manager import ConfigManager

    class SlowBackend(FakeBluetoothBackend):
        async def is_available(self) -> bool:
            await asyncio.Event().wait()
            return True

    monkeypatch.setattr(cli_module, "_DOCTOR_PROBE_TIMEOUT", 0.001)

    async def run() -> None:
        service = BlueChat(
            "Divin",
            config_manager=ConfigManager(tmp_path / "doctor.toml"),
            bluetooth=BluetoothManager(SlowBackend()),
        )
        await cli_module._doctor(service)

    asyncio.run(run())
    assert "TimeoutError" in capsys.readouterr().out


def test_doctor_renders_with_legacy_windows_encoding(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import io
    import bluechat.cli as cli_module
    from rich.console import Console
    from bluechat.bluetooth.manager import BluetoothManager

    output = io.BytesIO()
    text_output = io.TextIOWrapper(output, encoding="cp1252")
    monkeypatch.setattr(cli_module, "console", Console(file=text_output, width=120))

    async def run() -> None:
        service = BlueChat(
            "Divin",
            config_manager=ConfigManager(tmp_path / "doctor-legacy.toml"),
            bluetooth=BluetoothManager(FakeBluetoothBackend()),
        )
        await cli_module._doctor(service)

    asyncio.run(run())
    text_output.flush()
    rendered = output.getvalue().decode("cp1252")
    assert "BlueChat Diagnostics" in rendered
    assert "Bluetooth adapter" in rendered
    text_output.close()


def test_bluez_adapter_detection_and_powered_state_use_dbus(monkeypatch) -> None:
    pytest.importorskip("dbus_next")
    import asyncio
    from types import SimpleNamespace
    import dbus_next.aio
    from bluechat.bluetooth.linux import LinuxBluetoothBackend

    class Bus:
        def __init__(self) -> None:
            self.powered = True
            self.disconnected = False

        async def connect(self):
            return self

        async def introspect(self, _destination, _path):
            return object()

        def get_proxy_object(self, _destination, _path, _introspection):
            return SimpleNamespace(
                get_interface=lambda _name: SimpleNamespace(
                    call_get_managed_objects=self.managed_objects
                )
            )

        async def managed_objects(self):
            return {
                "/org/bluez/hci0": {
                    "org.bluez.Adapter1": {"Powered": SimpleNamespace(value=self.powered)}
                }
            }

        def disconnect(self):
            self.disconnected = True

    bus = Bus()
    monkeypatch.setattr(dbus_next.aio, "MessageBus", lambda **_kwargs: bus)

    async def run() -> None:
        backend = LinuxBluetoothBackend()
        assert await backend.is_available()
        assert await backend.is_enabled()
        assert bus.disconnected
        bus.powered = False
        assert not await backend.is_enabled()
        assert bus.disconnected

    asyncio.run(run())


def test_windows_backend_exposes_native_host_capabilities() -> None:
    capabilities = WindowsBluetoothBackend().capabilities
    assert capabilities.discovery and capabilities.hosting and capabilities.advertising
    assert capabilities.multiple_peers and capabilities.max_peers == 4


def test_windows_gatt_connection_reassembles_fragments_without_winrt_imports() -> None:
    import asyncio

    class StubServer:
        pass

    async def run() -> None:
        connection = WindowsGattConnection("peer", StubServer())  # type: ignore[arg-type]
        for fragment in fragment_packet(b"windows payload", fragment_size=4, frame_id=10):
            connection.feed_fragment(fragment)
        assert await connection.receive() == b"windows payload"
        connection.mark_disconnected()
        with pytest.raises(ConnectionError):
            await connection.receive()

    asyncio.run(run())


def test_windows_peripheral_subscriber_disconnect_lifecycle() -> None:
    import asyncio
    from types import SimpleNamespace
    from bluechat.bluetooth.windows_peripheral import WindowsGattPeripheral

    async def run() -> None:
        peripheral = WindowsGattPeripheral()
        subscriber = SimpleNamespace(
            session=SimpleNamespace(device_id=SimpleNamespace(id="peer-1"))
        )
        peripheral._apply_subscribers([subscriber])
        connection = await peripheral.accept()
        assert connection.peer_id == "peer-1"
        peripheral._apply_subscribers([])
        with pytest.raises(ConnectionError):
            await connection.receive()
        await peripheral.stop()

    asyncio.run(run())


def test_windows_advertising_callback_and_shutdown_cleanup() -> None:
    import asyncio
    from types import SimpleNamespace
    from bluechat.bluetooth.windows_peripheral import WindowsGattPeripheral

    class Provider:
        def __init__(self) -> None:
            self.stopped = 0

        def stop_advertising(self) -> None:
            self.stopped += 1

    async def run() -> None:
        backend = WindowsGattPeripheral()
        provider = Provider()
        backend._provider = provider
        backend._advertisement_status_changed(SimpleNamespace(advertisement_status="Started"), None)
        await asyncio.sleep(0)
        assert backend._advertisement_started.is_set()
        await backend.stop()
        assert provider.stopped == 1
        assert backend._provider is None
        assert backend._stopped

    asyncio.run(run())
