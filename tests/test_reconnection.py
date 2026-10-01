from __future__ import annotations

import asyncio
import time
from pathlib import Path

from bluechat.app import BlueChat
from bluechat.bluetooth.base import (
    BluetoothCapabilities,
    BluetoothDevice,
    BluetoothTransport,
    Connection,
    PairResult,
)
from bluechat.bluetooth.manager import BluetoothManager
from bluechat.chat.group import GroupRouter
from bluechat.chat.session import AsyncChatSession
from bluechat.chat.room import Room
from bluechat.config.manager import ConfigManager
from bluechat.config.models import HistoryPreference
from bluechat.errors import BluetoothUnavailableError
from bluechat.history.manager import HistoryManager
from bluechat.cli import (
    _accept_after_host_restart,
    _expire_reconnecting_participant,
    _monitor_host_service,
)
from bluechat.security.handshake import establish_test_pair
from bluechat.security.handshake import perform_client_handshake
from bluechat.security.resume import ResumeRegistry
from bluechat.cli import _accept_group_members, _reconnect_client, _wait_private_resume


class MemoryConnection:
    def __init__(self, peer_id: str) -> None:
        self.peer_id = peer_id
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.peer: MemoryConnection | None = None
        self.closed = False

    async def send(self, data: bytes) -> None:
        if self.closed or self.peer is None or self.peer.closed:
            raise ConnectionError("memory peer disconnected")
        await self.peer.incoming.put(data)

    async def receive(self) -> bytes:
        data = await self.incoming.get()
        if data is None:
            raise ConnectionError("memory peer disconnected")
        return data

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.peer is not None and not self.peer.closed:
            await self.peer.incoming.put(None)


def memory_pair(peer_id: str) -> tuple[MemoryConnection, MemoryConnection]:
    left, right = MemoryConnection(peer_id), MemoryConnection(peer_id)
    left.peer, right.peer = right, left
    return left, right


class MemoryTransport(BluetoothTransport):
    def __init__(self) -> None:
        self.accepted: asyncio.Queue[Connection] = asyncio.Queue()
        self.next_pair = 0
        self.host_failure = asyncio.Event()
        self.available = True
        self.enabled = True
        self.server_starts = 0
        self.server_stops = 0
        self.advertisements = 0

    @property
    def capabilities(self) -> BluetoothCapabilities:
        return BluetoothCapabilities(True, True, True, True, True, max_peers=4)

    async def is_available(self) -> bool:
        return self.available

    async def is_enabled(self) -> bool:
        return self.enabled

    async def discover(self, timeout: float = 8.0):
        return []

    async def advertise(self, room_metadata: dict[str, object]) -> None:
        self.advertisements += 1
        return None

    async def stop_advertising(self) -> None:
        return None

    async def connect(self, _device: BluetoothDevice) -> Connection:
        self.next_pair += 1
        host, client = memory_pair(f"peer-{self.next_pair}")
        await self.accepted.put(host)
        return client

    async def start_server(self) -> None:
        self.server_starts += 1
        return None

    async def stop_server(self) -> None:
        self.server_stops += 1
        return None

    async def wait_host_failure(self) -> None:
        await self.host_failure.wait()
        self.host_failure.clear()

    async def accept(self) -> Connection:
        return await self.accepted.get()

    async def pair(self, _device: BluetoothDevice) -> PairResult:
        return PairResult.PAIRED

    async def disconnect(self, _peer_id: str) -> None:
        return None


async def _live_group_resume_restores_same_participant_and_fresh_secure_session(
    tmp_path: Path, monkeypatch
) -> None:
    from bluechat import cli

    transport = MemoryTransport()
    service = BlueChat(
        "Host",
        config_manager=ConfigManager(tmp_path / "host.toml"),
        bluetooth=BluetoothManager(transport),
    )
    room = Room("Host", group=True)
    router = GroupRouter(room.room_id)
    registry = ResumeRegistry()
    tokens: dict[str, str] = {}
    deadlines: dict[str, float] = {}
    timeout_tasks: dict[str, asyncio.Task[None]] = {}

    async def peer_left(participant_id: str, _name: str) -> None:
        token = tokens[participant_id]
        assert registry.suspend(token, room.room_id, participant_id, room.session_id)
        deadlines[participant_id] = time.monotonic() + 30

    router.on_peer_left = peer_left
    monkeypatch.setattr(cli.typer, "confirm", lambda *_args, **_kwargs: True)
    accepting = asyncio.create_task(
        _accept_group_members(
            service,
            room,
            router,
            transport,
            resume_registry=registry,
            resume_tokens=tokens,
            reconnect_deadlines=deadlines,
            reconnect_tasks=timeout_tasks,
        )
    )

    device = BluetoothDevice(name="Host", identifier="host-device", bluechat_host=True)
    host_connection, guest_connection = memory_pair("first")
    await transport.accepted.put(host_connection)
    await guest_connection.send(b"BC-INIT")
    guest_keys = await perform_client_handshake(guest_connection, room.code)
    guest_session = AsyncChatSession(
        "Guest", "Host", guest_connection, guest_keys, room_id=room.room_id
    )
    guest_session.start()
    await guest_session.send_control("HELLO", {"username": "Guest"})
    joined = await guest_session.receive_control("JOIN_ACCEPT", timeout=3)
    participant_id = joined.payload["participant_id"]
    original_token = joined.payload["resume_token"]
    assert room.participant_names[participant_id] == "Guest"

    second_host_connection, other_connection = memory_pair("second")
    await transport.accepted.put(second_host_connection)
    await other_connection.send(b"BC-INIT")
    other_keys = await perform_client_handshake(other_connection, room.code)
    other_session = AsyncChatSession(
        "Other", "Host", other_connection, other_keys, room_id=room.room_id
    )
    other_session.start()
    await other_session.send_control("HELLO", {"username": "Other"})
    other_joined = await other_session.receive_control("JOIN_ACCEPT", timeout=3)
    other_id = other_joined.payload["participant_id"]
    other_session.participant_id = other_id
    other_session.allow_remote_senders = True
    assert other_id in room.participant_names

    # A transport loss suspends the active peer and starts the 30-second window.
    await guest_connection.close()
    for _ in range(100):
        if participant_id in deadlines:
            break
        await asyncio.sleep(0.01)
    assert participant_id in deadlines
    assert participant_id in room.participant_names
    await other_session.send_text("room continues")
    while True:
        delivered_before_resume = await asyncio.wait_for(router.incoming.get(), timeout=2)
        if delivered_before_resume.text == "room continues":
            break

    # The same participant reconnects using the issued short-lived credential.
    context: dict[str, object] = {
        "device": device,
        "room_id": room.room_id,
        "session_id": room.session_id,
        "participant_id": participant_id,
        "resume_token": original_token,
        "username": "Guest",
        "host": "Host",
    }
    resumed = await asyncio.wait_for(_reconnect_client(service, context), timeout=4)
    assert resumed is not None
    assert deadlines.get(participant_id) is None
    assert room.participant_names[participant_id] == "Guest"
    assert context["resume_token"] != original_token

    await resumed.send_text("back online")
    delivered = await asyncio.wait_for(router.incoming.get(), timeout=2)
    assert delivered.text == "back online"
    assert delivered.sender_id == participant_id

    await resumed.close()
    await other_session.close()
    accepting.cancel()
    await asyncio.gather(accepting, return_exceptions=True)
    await router.close()


def test_live_group_resume_restores_same_participant_and_fresh_secure_session(
    tmp_path: Path, monkeypatch
) -> None:
    asyncio.run(
        _live_group_resume_restores_same_participant_and_fresh_secure_session(tmp_path, monkeypatch)
    )


def test_resume_registry_rejects_wrong_session_without_consuming_valid_token() -> None:
    async def run() -> None:
        registry = ResumeRegistry()
        token = registry.issue("room", "guest", "session")
        assert not registry.verify(token, "room", "guest", "other-session")
        assert registry.verify(token, "room", "guest", "session")
        assert registry.consume(token, "room", "guest", "session")
        assert not registry.consume(token, "room", "guest", "session")

    asyncio.run(run())


def test_expired_group_reconnect_removes_roster_token_and_records_leave(tmp_path: Path) -> None:
    async def run() -> None:
        room = Room("Host", group=True)
        participant_id = room.request_join("Guest", room.code, approved=True)
        router = GroupRouter(room.room_id)
        host_observer, observer_connection = memory_pair("observer")
        host_keys, observer_keys = establish_test_pair("M7KP4X")
        host_session = AsyncChatSession(
            "Host", "Observer", host_observer, host_keys, room_id=room.room_id
        )
        observer_session = AsyncChatSession(
            "Observer",
            "Host",
            observer_connection,
            observer_keys,
            participant_id="observer",
            room_id=room.room_id,
            allow_remote_senders=True,
        )
        host_session.start()
        observer_session.start()
        await router.add("observer", "Observer", host_session)
        history = HistoryManager(tmp_path / "history")
        path = history.create("group", room_id=room.room_id, preference=HistoryPreference.ALWAYS)
        deadlines = {participant_id: 30.0}
        tokens = {participant_id: "short-lived-secret"}
        observed_sleep: list[float] = []

        async def advance(seconds: float) -> None:
            observed_sleep.append(seconds)

        expired = await _expire_reconnecting_participant(
            room,
            router,
            history,
            path,
            deadlines,
            tokens,
            participant_id,
            "Guest",
            30.0,
            clock=lambda: 0.0,
            sleep=advance,
        )
        assert expired
        assert observed_sleep == [30.0]
        assert participant_id not in room.participant_names
        assert participant_id not in deadlines and participant_id not in tokens
        assert path is not None and '"event":"left"' in path.read_text()
        left_event = await observer_session.receive_control("USER_LEFT", timeout=1)
        assert left_event.payload.get("participant_id") == participant_id
        await router.close()
        await observer_session.close()

    asyncio.run(run())


def test_client_reconnect_retries_within_bounded_deadline_and_returns_cleanly(tmp_path) -> None:
    async def run() -> None:
        class FailingTransport(MemoryTransport):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0

            async def connect(self, _device: BluetoothDevice) -> Connection:
                self.attempts += 1
                raise BluetoothUnavailableError("host is temporarily unavailable")

        transport = FailingTransport()
        service = BlueChat(
            "Guest",
            config_manager=ConfigManager(tmp_path / "guest.toml"),
            bluetooth=BluetoothManager(transport),
        )
        current = [0.0]

        async def advance(seconds: float) -> None:
            current[0] += seconds

        result = await _reconnect_client(
            service,
            {
                "device": BluetoothDevice(name="Host", identifier="host"),
                "room_id": "room",
                "session_id": "session",
                "participant_id": "participant",
                "resume_token": "x" * 40,
                "username": "Guest",
                "host": "Host",
            },
            window_seconds=30,
            clock=lambda: current[0],
            sleep=advance,
        )
        assert result is None
        assert current[0] == 30
        assert transport.attempts >= 7

    asyncio.run(run())


def test_live_private_resume_uses_bound_credential_and_restores_chat(tmp_path: Path) -> None:
    async def run() -> None:
        transport = MemoryTransport()
        service = BlueChat(
            "Host",
            config_manager=ConfigManager(tmp_path / "private-host.toml"),
            bluetooth=BluetoothManager(transport),
        )
        room = Room("Host", group=False)
        participant_id = room.request_join("Guest", room.code, approved=True)
        registry = ResumeRegistry()
        token = registry.issue(room.room_id, participant_id, room.session_id, deferred_expiry=True)
        assert registry.suspend(token, room.room_id, participant_id, room.session_id)
        tokens = {participant_id: token}
        device = BluetoothDevice(name="Host", identifier="host")
        client_context: dict[str, object] = {
            "device": device,
            "room_id": room.room_id,
            "session_id": room.session_id,
            "participant_id": participant_id,
            "resume_token": token,
            "username": "Guest",
            "host": "Host",
            "group": False,
        }

        host_task = asyncio.create_task(
            _wait_private_resume(
                service,
                room,
                transport,
                registry,
                tokens,
                participant_id,
                "Guest",
            )
        )
        client_session = await asyncio.wait_for(
            _reconnect_client(service, client_context), timeout=3
        )
        host_session = await asyncio.wait_for(host_task, timeout=3)
        assert client_session is not None and host_session is not None
        assert client_context["resume_token"] == tokens[participant_id]
        assert client_context["resume_token"] != token
        assert not registry.verify(token, room.room_id, participant_id, room.session_id)
        await client_session.send_text("private again")
        received = await asyncio.wait_for(host_session.receive(), timeout=2)
        assert received.text == "private again"
        await client_session.close()
        await host_session.close()

    asyncio.run(run())


def test_private_host_resume_window_expiry_returns_no_session(tmp_path: Path) -> None:
    async def run() -> None:
        transport = MemoryTransport()
        service = BlueChat(
            "Host",
            config_manager=ConfigManager(tmp_path / "expired-host.toml"),
            bluetooth=BluetoothManager(transport),
        )
        room = Room("Host", group=False)
        participant_id = room.request_join("Guest", room.code, approved=True)
        registry = ResumeRegistry()
        tokens = {
            participant_id: registry.issue(
                room.room_id, participant_id, room.session_id, deferred_expiry=True
            )
        }
        result = await _wait_private_resume(
            service,
            room,
            transport,
            registry,
            tokens,
            participant_id,
            "Guest",
            window_seconds=0,
        )
        assert result is None

    asyncio.run(run())


def test_host_transport_recovers_service_and_retains_room_state(
    tmp_path: Path, monkeypatch
) -> None:
    async def run() -> None:
        transport = MemoryTransport()
        transport.enabled = False
        service = BlueChat(
            "Host",
            config_manager=ConfigManager(tmp_path / "recover-host.toml"),
            bluetooth=BluetoothManager(transport),
        )
        room = Room("Host", group=True)
        original_room_id, original_code = room.room_id, room.code
        restart_event, room_ended = asyncio.Event(), asyncio.Event()
        monkeypatch.setattr("bluechat.cli._HOST_RECOVERY_POLL", 0.001)
        monitor = asyncio.create_task(
            _monitor_host_service(
                service,
                room,
                transport,
                restart_event,
                room_ended,
                window_seconds=1,
            )
        )
        await asyncio.sleep(0)
        transport.host_failure.set()
        for _ in range(100):
            if transport.server_stops:
                break
            await asyncio.sleep(0.001)
        assert transport.server_stops == 1
        transport.enabled = True
        await asyncio.wait_for(restart_event.wait(), timeout=1)
        assert transport.server_starts == 1
        assert transport.advertisements == 1
        assert room.room_id == original_room_id and room.code == original_code
        assert not room_ended.is_set()
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)

    asyncio.run(run())


def test_host_service_recovery_timeout_ends_room(tmp_path: Path, monkeypatch) -> None:
    async def run() -> None:
        transport = MemoryTransport()
        transport.enabled = False
        service = BlueChat(
            "Host",
            config_manager=ConfigManager(tmp_path / "lost-host.toml"),
            bluetooth=BluetoothManager(transport),
        )
        room = Room("Host", group=True)
        restart_event, room_ended = asyncio.Event(), asyncio.Event()
        monkeypatch.setattr("bluechat.cli._HOST_RECOVERY_POLL", 0.001)
        monitor = asyncio.create_task(
            _monitor_host_service(
                service,
                room,
                transport,
                restart_event,
                room_ended,
                window_seconds=0.005,
            )
        )
        await asyncio.sleep(0)
        transport.host_failure.set()
        await asyncio.wait_for(room_ended.wait(), timeout=1)
        assert transport.server_stops == 1
        assert transport.server_starts == 0
        assert not restart_event.is_set()
        await monitor

    asyncio.run(run())


def test_blocked_host_accept_rebinds_after_server_restart() -> None:
    async def run() -> None:
        transport = MemoryTransport()
        restart_event, room_ended = asyncio.Event(), asyncio.Event()
        accepting = asyncio.create_task(
            _accept_after_host_restart(transport, restart_event, room_ended)
        )
        await asyncio.sleep(0)
        restart_event.set()
        await asyncio.sleep(0)
        host_connection, _client = memory_pair("after-restart")
        await transport.accepted.put(host_connection)
        restored = await asyncio.wait_for(accepting, timeout=1)
        assert restored is host_connection

    asyncio.run(run())
