"""End-to-end group workflow over in-memory Bluetooth connections."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bluechat.chat.group import GroupRouter
from bluechat.chat.room import Room
from bluechat.chat.session import AsyncChatSession
from bluechat.history.manager import HistoryManager
from bluechat.config.models import HistoryPreference
from bluechat.errors import AuthenticationError, RoomFullError
from bluechat.security.handshake import establish_test_pair
from bluechat.security.resumption import client_resume, host_resume
from bluechat.security.resume import ResumeRegistry
from bluechat.transfer.group import GroupFileRelay
from bluechat.transfer.protocol import FileTransferManager


class Link:
    def __init__(self, peer_id: str) -> None:
        self.peer_id = peer_id
        self.incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.peer: Link | None = None
        self.closed = False

    async def send(self, data: bytes) -> None:
        if self.closed or self.peer is None or self.peer.closed:
            raise ConnectionError("in-memory Bluetooth peer disconnected")
        await self.peer.incoming.put(data)

    async def receive(self) -> bytes:
        packet = await self.incoming.get()
        if packet is None:
            raise ConnectionError("in-memory Bluetooth peer disconnected")
        return packet

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.peer is not None and not self.peer.closed:
            await self.peer.incoming.put(None)


def link_pair(peer_id: str) -> tuple[Link, Link]:
    host, client = Link(peer_id), Link(peer_id)
    host.peer, client.peer = client, host
    return host, client


async def make_sessions(
    name: str, participant_id: str, room_id: str
) -> tuple[AsyncChatSession, AsyncChatSession]:
    host_link, client_link = link_pair(participant_id)
    host_keys, client_keys = establish_test_pair("M7KP4X")
    host = AsyncChatSession(
        "Divin",
        name,
        host_link,
        host_keys,
        participant_id=f"host-{participant_id}",
        room_id=room_id,
    )
    client = AsyncChatSession(
        name,
        "Divin",
        client_link,
        client_keys,
        participant_id=participant_id,
        room_id=room_id,
        allow_remote_senders=True,
    )
    host.start()
    client.start()
    return host, client


def test_complete_group_transfer_reconnect_rotation_history_workflow(tmp_path: Path) -> None:
    async def run() -> None:
        from bluechat.cli import _rotate_join_code

        room = Room("Divin", group=True)
        router = GroupRouter(room.room_id)
        relay = GroupFileRelay(router, peer_timeout=2)
        router.on_control = relay.handle
        history = HistoryManager(tmp_path / "history")
        host_history = history.create(
            "group", room_id=room.room_id, preference=HistoryPreference.ALWAYS
        )
        assert host_history is not None

        clients: dict[str, AsyncChatSession] = {}
        host_sessions: dict[str, AsyncChatSession] = {}
        participants: dict[str, str] = {}
        for name in ("Alex", "Grace", "Bob"):
            participant_id = room.request_join(name, room.code, approved=True)
            participants[name] = participant_id
            host, client = await make_sessions(name, participant_id, room.room_id)
            host_sessions[name] = host
            clients[name] = client
            await router.add(participant_id, name, host)
            history.append(host_history, "joined", sender=name)

        assert len(room.participants) == 4  # Host plus three guests.
        await clients["Alex"].send_text("hello group")
        routed = await asyncio.wait_for(router.incoming.get(), timeout=2)
        assert routed.text == "hello group"
        assert routed.sender_id == participants["Alex"]
        assert (await asyncio.wait_for(clients["Grace"].receive(), timeout=2)).text == "hello group"
        assert (await asyncio.wait_for(clients["Bob"].receive(), timeout=2)).text == "hello group"
        history.append(host_history, "message", sender="Alex", text="hello group")

        # Each recipient decides independently; only Grace accepts the image.
        source = tmp_path / "room image.png"
        data = b"bluechat image bytes" * 700
        source.write_bytes(data)
        files = FileTransferManager(chunk_size=512)

        async def receive(name: str, *, accept: bool) -> Path | None:
            client = clients[name]
            offer = await client.receive_control("FILE_OFFER", timeout=3)

            async def approval(_filename: str, _size: int, _mime: str) -> Path | None:
                return tmp_path / name if accept else None

            return await files.receive_file(client, offer, approval)

        grace_receive = asyncio.create_task(receive("Grace", accept=True))
        bob_receive = asyncio.create_task(receive("Bob", accept=False))
        transfer_id = await asyncio.wait_for(files.send_file(clients["Alex"], source), timeout=8)
        saved, declined = await asyncio.gather(grace_receive, bob_receive)
        assert transfer_id
        assert saved is not None and saved.read_bytes() == data
        assert declined is None
        history.append(
            host_history,
            "file",
            sender="Alex",
            metadata={"filename": source.name, "size": len(data)},
        )

        # Grace drops; the other members keep chatting while her roster entry is
        # retained for a single-use, room/session/participant-bound resume.
        grace_id = participants["Grace"]
        registry = ResumeRegistry()
        token = registry.issue(room.room_id, grace_id, room.session_id, deferred_expiry=True)
        assert registry.suspend(token, room.room_id, grace_id, room.session_id)
        await clients["Grace"].close()
        for _ in range(100):
            if grace_id not in router.peer_sessions:
                break
            await asyncio.sleep(0.01)
        assert grace_id not in router.peer_sessions
        await clients["Bob"].send_text("room continues")
        assert (await asyncio.wait_for(clients["Alex"].receive(), timeout=2)).text == "room continues"
        continued = await asyncio.wait_for(router.incoming.get(), timeout=2)
        assert continued.text == "room continues"

        host_link, grace_link = link_pair("grace-resume")
        host_resume_task = asyncio.create_task(
            host_resume(
                host_link,
                registry,
                room.session_id,
                lambda requested_room, requested_peer: (
                    token
                    if requested_room == room.room_id and requested_peer == grace_id
                    else None
                ),
            )
        )
        grace_keys = await client_resume(
            grace_link, token, room.room_id, room.session_id, grace_id
        )
        host_keys, resumed_room, resumed_id = await asyncio.wait_for(host_resume_task, timeout=2)
        assert (resumed_room, resumed_id) == (room.room_id, grace_id)
        assert not registry.verify(token, room.room_id, grace_id, room.session_id)
        host_resumed = AsyncChatSession(
            "Divin", "Grace", host_link, host_keys, participant_id=f"host-{grace_id}",
            room_id=room.room_id,
        )
        client_resumed = AsyncChatSession(
            "Grace", "Divin", grace_link, grace_keys, participant_id=grace_id,
            room_id=room.room_id, allow_remote_senders=True,
        )
        host_resumed.start()
        client_resumed.start()
        await router.add(grace_id, "Grace", host_resumed)
        clients["Grace"] = client_resumed
        history.append(host_history, "joined", sender="Grace", metadata={"resumed": True})
        await clients["Grace"].send_text("back online")
        restored = await asyncio.wait_for(router.incoming.get(), timeout=2)
        assert (restored.sender_id, restored.text) == (grace_id, "back online")

        # Rotate the join code without disturbing current participants, admit a
        # fourth guest into the five-person room, and reject any further join.
        old_code = room.code
        new_code = _rotate_join_code(room)
        assert new_code != old_code
        room.validate_code(new_code)
        with pytest.raises(AuthenticationError):
            room.validate_code(old_code)
        dave_id = room.request_join("Dave", new_code, approved=True)
        dave_host, dave_client = await make_sessions("Dave", dave_id, room.room_id)
        host_sessions["Dave"] = dave_host
        clients["Dave"] = dave_client
        participants["Dave"] = dave_id
        await router.add(dave_id, "Dave", dave_host)
        with pytest.raises(RoomFullError):
            room.request_join("Eve", new_code, approved=True)
        assert len(room.participants) == 5

        history.append(host_history, "left", sender="Bob")
        history_text = host_history.read_text(encoding="utf-8")
        assert '"resumed":true' in history_text
        assert '"filename":"room image.png"' in history_text
        assert "M7KP4X" not in history_text
        assert old_code not in history_text and new_code not in history_text

        # Deterministic cleanup covers all remaining connections and relay tasks.
        await relay.close()
        await router.close()
        await asyncio.gather(*(client.close() for client in clients.values()), return_exceptions=True)

    asyncio.run(run())
