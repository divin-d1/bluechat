"""Typer CLI. Commands delegate to the public BlueChat application facade."""

from __future__ import annotations

import logging
import asyncio
import platform
import secrets
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

import typer
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout

from bluechat import __version__
from bluechat.app import BlueChat
from bluechat.bluetooth.base import BluetoothDevice, BluetoothTransport, Connection
from bluechat.chat.session import AsyncChatSession
from bluechat.chat.session import ChatMessage
from bluechat.chat.group import GroupRouter
from bluechat.chat.room import Room
from bluechat.config.models import HistoryPreference
from bluechat.history.manager import HistoryManager
from bluechat.transfer.files import file_category, safe_filename
from bluechat.transfer.protocol import FileTransferManager
from bluechat.transfer.group import GroupFileRelay
from bluechat.errors import (
    BlueChatError,
    BluetoothPermissionError,
    BluetoothUnavailableError,
    ConfigurationError,
)
from bluechat.errors import AuthenticationError, ProtocolError, RoomExpiredError
from bluechat.errors import AuthenticationRateLimitedError
from bluechat.errors import TransferError
from bluechat.protocol.messages import Message
from bluechat.security.handshake import (
    perform_client_handshake,
    perform_host_handshake,
    validate_room_code,
)
from bluechat.security.resume import ResumeRegistry
from bluechat.security.resumption import host_resume, client_resume
from bluechat.ui.console import console
from bluechat.ui.countdown import _room_panel
from bluechat.ui.countdown import format_countdown
from bluechat.utils.validation import validate_username

app = typer.Typer(
    name="bluechat",
    help="Private, nearby chat over Bluetooth.",
    no_args_is_help=False,
    add_completion=False,
    invoke_without_command=True,
)
_DEBUG = False
_DOCTOR_PROBE_TIMEOUT = 8.0
_HOST_RECOVERY_WINDOW = 30.0
_HOST_RECOVERY_POLL = 1.0
_HOST_PROBE_TIMEOUT = 2.0


def _show_version(value: bool) -> None:
    if value:
        console.print(__version__)
        raise typer.Exit()


def _service() -> BlueChat:
    try:
        service = BlueChat()
    except ConfigurationError as exc:
        console.print(f"[red]Configuration problem:[/red] {exc}")
        raise typer.Exit(2) from exc
    _ensure_profile(service)
    return service


def _ensure_profile(service: BlueChat) -> None:
    if service.username:
        return
    console.print(
        Panel("[bold]Welcome to BlueChat[/bold]\nBluetooth messaging for nearby computers")
    )
    while True:
        try:
            name = validate_username(typer.prompt("Choose your username"))
            service.set_username(name)
            return
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
        except (EOFError, KeyboardInterrupt) as exc:
            console.print("\n[dim]Setup cancelled.[/dim]")
            raise typer.Exit(1) from exc
        except typer.Abort as exc:
            console.print("\n[dim]Setup cancelled.[/dim]")
            raise typer.Exit(1) from exc


def _friendly_error(exc: BlueChatError) -> None:
    console.print(f"[red]BlueChat:[/red] {exc}")
    raise typer.Exit(1) from exc


def _unexpected_error(exc: Exception) -> None:
    logging.getLogger(__name__).exception("Unexpected CLI failure")
    console.print("[red]BlueChat encountered an unexpected problem.[/red]")
    if _DEBUG:
        console.print_exception(show_locals=False)
    else:
        console.print("Run again with [bold]--debug[/bold] for diagnostic details.")
    raise typer.Exit(1) from exc


@app.callback()
def main(
    ctx: typer.Context,
    debug: bool = typer.Option(False, "--debug", help="Show detailed diagnostics."),
    show_version: bool = typer.Option(
        False,
        "--version",
        callback=lambda value: _show_version(value),
        is_eager=True,
        help="Show the installed version and exit.",
    ),
) -> None:
    """Start the interactive menu when no command is given."""
    global _DEBUG
    _DEBUG = debug
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING)
    del show_version
    if ctx.invoked_subcommand is None:
        service = _service()
        _menu(service)


def _menu(service: BlueChat) -> None:
    console.print(Panel.fit("[bold]BlueChat[/bold]\nBluetooth messaging", border_style="blue"))
    console.print(f"Username: [bold]{service.username}[/bold]")
    try:
        ready = asyncio.run(service.bluetooth.transport.is_available())
        console.print(f"Bluetooth: {'Ready' if ready else 'Unavailable'}")
    except BluetoothPermissionError as exc:
        console.print(f"Bluetooth: [yellow]Permission needed[/yellow] ({exc})")
    except BlueChatError as exc:
        console.print(f"Bluetooth: [yellow]Unavailable[/yellow] ({exc})")
    table = Table(show_header=False, box=None, padding=(0, 2))
    for shortcut, label in (
        ("1", "Host a chat"),
        ("2", "Join a chat"),
        ("3", "Nearby devices"),
        ("4", "Settings"),
        ("5", "About"),
        ("6", "Exit"),
    ):
        table.add_row(f"[cyan][{shortcut}][/cyan]", label)
    console.print(table)
    try:
        choice = typer.prompt("Select", default="6")
    except (EOFError, KeyboardInterrupt):
        return
    except typer.Abort:
        return
    if choice in {"1", "2", "3"}:
        if choice == "3":
            devices()
        elif choice == "1":
            asyncio.run(_host(service, group=False))
        else:
            asyncio.run(_join(service))
    elif choice == "4":
        config(None)
    elif choice == "5":
        info()


@app.command()
def host(
    group: bool = typer.Option(False, "--group"),
    private: bool = typer.Option(False, "--private"),
) -> None:
    """Create a private or group room."""
    del private  # Private is the default; kept as an explicit convenience flag.
    asyncio.run(_host(_service(), group=group))


async def _host(service: BlueChat, *, group: bool) -> None:
    if group:
        await _host_group(service)
        return
    transport = service.bluetooth.transport
    room = None
    resume_registry = ResumeRegistry()
    resume_tokens: dict[str, str] = {}
    restart_event = asyncio.Event()
    room_ended = asyncio.Event()
    recovery_task: asyncio.Task[None] | None = None
    try:
        await service.bluetooth.require_ready(role="host")
        room = service.create_room(group=False)
        await transport.start_server()
        try:
            await transport.advertise({"device_name": service.username, "room_id": room.room_id})
            recovery_task = asyncio.create_task(
                _monitor_host_service(service, room, transport, restart_event, room_ended),
                name="bluechat-host-service-recovery",
            )
            console.print(f"Hosting private room on {service.username}.")
            while True:
                connection = await _wait_for_room_peer(room, transport, restart_event, room_ended)
                if connection is None:
                    console.print("[yellow]Room code expired; no participant joined.[/yellow]")
                    return
                session: AsyncChatSession | None = None
                try:
                    initial = await asyncio.wait_for(connection.receive(), timeout=10.0)
                    if initial != b"BC-INIT":
                        raise AuthenticationError("Peer did not begin the BlueChat handshake")
                    room.check_auth_rate_limit()
                    keys = await asyncio.wait_for(
                        perform_host_handshake(connection, room.code), timeout=30.0
                    )
                    room.record_auth_success()
                    session = AsyncChatSession(
                        service.username or "Host", None, connection, keys, room_id=room.room_id
                    )
                    session.start()
                    hello = await session.receive_control("HELLO", timeout=15.0)
                    guest = _validated_payload_username(hello.payload, "username")
                    room.validate_code(room.code)
                    accepted = typer.confirm(f"{guest} wants to join. Accept?", default=False)
                    if not accepted:
                        await session.send_control(
                            "JOIN_REJECT", {"reason": "Host declined the request"}
                        )
                        console.print("Join request declined; the room remains open.")
                        await session.close()
                        continue
                    participant_id = room.request_join(guest, room.code, approved=True)
                    session.peer_username = guest
                    session.participant_id = "host"
                    resume_token = resume_registry.issue(
                        room.room_id,
                        participant_id,
                        room.session_id,
                        deferred_expiry=True,
                    )
                    resume_tokens[participant_id] = resume_token
                    await session.send_control(
                        "JOIN_ACCEPT",
                        {
                            "host": service.username,
                            "room_id": room.room_id,
                            "session_id": room.session_id,
                            "participant_id": participant_id,
                            "group": False,
                            "resume_token": resume_token,
                            "participants": [
                                {"participant_id": pid, "username": name}
                                for pid, name in room.list_participants()
                            ],
                        },
                    )
                    console.print(
                        f"[green]{guest} joined. Encrypted private chat is ready.[/green]"
                    )
                    while True:
                        end_reason = await _chat_loop(
                            session,
                            service.username or "Host",
                            guest,
                            "Private",
                            service=service,
                            room_id=room.room_id,
                            host_name=service.username or "Host",
                            roster=dict(room.list_participants()),
                        )
                        if end_reason != "transport_lost":
                            return
                        if not resume_registry.suspend(
                            resume_token, room.room_id, participant_id, room.session_id
                        ):
                            console.print(
                                "[yellow]The private session could not be resumed securely.[/yellow]"
                            )
                            return
                        session = await _wait_private_resume(
                            service,
                            room,
                            transport,
                            resume_registry,
                            resume_tokens,
                            participant_id,
                            guest,
                            restart_event=restart_event,
                            room_ended=room_ended,
                        )
                        if session is None:
                            room.remove_participant(participant_id)
                            resume_tokens.pop(participant_id, None)
                            console.print(
                                "[yellow]The peer did not reconnect; the private room has ended.[/yellow]"
                            )
                            return
                        resume_token = resume_tokens[participant_id]
                except (AuthenticationError, asyncio.TimeoutError, ProtocolError) as exc:
                    if isinstance(exc, AuthenticationError) and not isinstance(
                        exc, AuthenticationRateLimitedError
                    ):
                        room.record_auth_failure()
                    console.print(f"[yellow]Join attempt failed:[/yellow] {exc}")
                    if session is not None:
                        await session.close()
                    else:
                        await connection.close()
                    if room.remaining_seconds() <= 0:
                        return
                except ValueError as exc:
                    console.print(
                        f"[yellow]Invalid join identity; request rejected ({exc}).[/yellow]"
                    )
                    if session is not None:
                        try:
                            await session.send_control(
                                "JOIN_REJECT", {"reason": "Invalid username"}
                            )
                        except BlueChatError as reject_error:
                            logging.getLogger(__name__).debug(
                                "Could not send rejection to invalid peer (%s)",
                                type(reject_error).__name__,
                            )
                        await session.close()
                    else:
                        await connection.close()
                    if room.remaining_seconds() <= 0:
                        return
        finally:
            if recovery_task is not None:
                recovery_task.cancel()
                await asyncio.gather(recovery_task, return_exceptions=True)
            await transport.stop_advertising()
            await transport.stop_server()
    except BlueChatError as exc:
        _friendly_error(exc)
    except (AuthenticationError, RoomExpiredError, ValueError) as exc:
        console.print(f"[red]BlueChat:[/red] {exc}")
    except asyncio.TimeoutError:
        console.print(
            "[yellow]The join request timed out. The room remains available until its code expires.[/yellow]"
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[dim]Room closed.[/dim]")
    except typer.Abort:
        console.print("\n[dim]Room closed.[/dim]")
    except Exception as exc:
        _unexpected_error(exc)


async def _host_group(service: BlueChat) -> None:
    """Host a routed group using one encrypted Bluetooth connection per peer."""
    transport = service.bluetooth.transport
    room: Room | None = None
    router: GroupRouter | None = None
    file_relay: GroupFileRelay | None = None
    resume_registry = ResumeRegistry()
    resume_tokens: dict[str, str] = {}
    reconnect_deadlines: dict[str, float] = {}
    reconnect_tasks: dict[str, asyncio.Task[None]] = {}
    intentional_leaves: set[str] = set()
    active_router: GroupRouter
    restart_event = asyncio.Event()
    room_ended = asyncio.Event()
    recovery_task: asyncio.Task[None] | None = None
    try:
        await service.bluetooth.require_ready(role="host")
        room = service.create_room(group=True)
        history = HistoryManager()
        consent = (
            typer.confirm("Save this group conversation locally?", default=False)
            if service.config.history is HistoryPreference.ASK
            else None
        )
        try:
            history_path = history.create(
                "group",
                room_id=room.room_id,
                preference=service.config.history,
                consent=consent,
            )
        except OSError as exc:
            history_path = None
            console.print(f"[yellow]Local history is unavailable ({type(exc).__name__}).[/yellow]")

        async def participant_left(participant_id: str, name: str) -> None:
            if participant_id in intentional_leaves:
                intentional_leaves.discard(participant_id)
                reconnect_deadlines.pop(participant_id, None)
                resume_tokens.pop(participant_id, None)
                room.remove_participant(participant_id)
                history.append(history_path, "left", sender=name)
                await active_router.broadcast_event(
                    "USER_LEFT", {"participant_id": participant_id, "username": name}
                )
                return
            token = resume_tokens.get(participant_id)
            if token is not None:
                resume_registry.suspend(token, room.room_id, participant_id, room.session_id)
            deadline = time.monotonic() + 30
            reconnect_deadlines[participant_id] = deadline
            console.print(
                f"[yellow]{name} disconnected; waiting up to 30 seconds to reconnect.[/yellow]"
            )
            await active_router.broadcast_event(
                "USER_DISCONNECTED", {"participant_id": participant_id, "username": name}
            )

            async def expire_participant() -> None:
                expired = await _expire_reconnecting_participant(
                    room,
                    active_router,
                    history,
                    history_path,
                    reconnect_deadlines,
                    resume_tokens,
                    participant_id,
                    name,
                    deadline,
                )
                if expired:
                    console.print(
                        f"[yellow]{name} left the room after the reconnect window.[/yellow]"
                    )

            prior = reconnect_tasks.pop(participant_id, None)
            if prior is not None:
                prior.cancel()
            reconnect_tasks[participant_id] = asyncio.create_task(expire_participant())

        router = GroupRouter(room.room_id, on_peer_left=participant_left)
        active_router = router
        file_relay = GroupFileRelay(router)

        async def handle_group_control(participant_id: str, message: Message) -> None:
            if message.type == "DISCONNECT":
                intentional_leaves.add(participant_id)
                await router.remove(participant_id)
                return
            if message.type == "FILE_OFFER":
                name = router.peer_sessions.get(participant_id)
                filename = message.payload.get("filename")
                size = message.payload.get("size")
                if isinstance(filename, str) and isinstance(size, int):
                    history.append(
                        history_path,
                        "file",
                        sender=name.peer_username if name else "participant",
                        metadata={
                            "filename": Path(filename).name,
                            "size": size,
                            "event": "offered",
                        },
                    )
            await file_relay.handle(participant_id, message)

        router.on_control = handle_group_control
        await transport.start_server()
        await transport.advertise({"device_name": service.username, "room_id": room.room_id})
        recovery_task = asyncio.create_task(
            _monitor_host_service(service, room, transport, restart_event, room_ended),
            name="bluechat-host-service-recovery",
        )
        console.print(
            Panel(f"Group room created\nCode: [bold]{room.code}[/bold]\nExpires in 05:00")
        )
        accept_task = asyncio.create_task(
            _accept_group_members(
                service,
                room,
                router,
                transport,
                history,
                history_path,
                resume_registry,
                resume_tokens,
                reconnect_deadlines,
                reconnect_tasks,
                restart_event,
                room_ended,
            )
        )
        await _group_host_prompt(
            service,
            room,
            router,
            accept_task,
            history,
            history_path,
            reconnect_deadlines,
            room_ended,
        )
    except BlueChatError as exc:
        _friendly_error(exc)
    except (KeyboardInterrupt, asyncio.CancelledError, typer.Abort):
        console.print("\n[dim]Room closed.[/dim]")
    except Exception as exc:
        _unexpected_error(exc)
    finally:
        if recovery_task is not None:
            recovery_task.cancel()
            await asyncio.gather(recovery_task, return_exceptions=True)
        if file_relay is not None:
            await file_relay.close()
        for reconnect_task in reconnect_tasks.values():
            reconnect_task.cancel()
        await asyncio.gather(*reconnect_tasks.values(), return_exceptions=True)
        if router is not None:
            await router.close()
        await transport.stop_advertising()
        await transport.stop_server()


async def _wait_private_resume(
    service: BlueChat,
    room: Room,
    transport: BluetoothTransport,
    registry: ResumeRegistry,
    resume_tokens: dict[str, str],
    participant_id: str,
    peer_name: str,
    *,
    window_seconds: float = 30.0,
    restart_event: asyncio.Event | None = None,
    room_ended: asyncio.Event | None = None,
) -> AsyncChatSession | None:
    """Keep a private room open for its existing peer's secure resume attempt."""
    deadline = time.monotonic() + window_seconds
    console.print(
        f"[yellow]Connection lost; waiting {int(window_seconds)} seconds for the peer to reconnect.[/yellow]"
    )
    while time.monotonic() < deadline and not (room_ended and room_ended.is_set()):
        remaining = deadline - time.monotonic()
        connection: Connection | None = None
        try:
            connection = await asyncio.wait_for(
                _accept_after_host_restart(transport, restart_event, room_ended),
                timeout=remaining,
            )
            first = await asyncio.wait_for(connection.receive(), timeout=min(10.0, remaining))
            keys, resumed_room, resumed_participant = await asyncio.wait_for(
                host_resume(
                    connection,
                    registry,
                    room.session_id,
                    lambda room_id, pid: (
                        resume_tokens.get(pid) if room_id == room.room_id else None
                    ),
                    first_message=first,
                ),
                timeout=min(10.0, remaining),
            )
            if (
                resumed_room != room.room_id
                or resumed_participant != participant_id
                or time.monotonic() >= deadline
                or room.participant_names.get(participant_id) != peer_name
            ):
                raise AuthenticationError("The private reconnect window or identity is invalid")
            next_token = registry.issue(
                room.room_id, participant_id, room.session_id, deferred_expiry=True
            )
            resume_tokens[participant_id] = next_token
            session = AsyncChatSession(
                service.username or "Host",
                peer_name,
                connection,
                keys,
                participant_id="host",
                room_id=room.room_id,
            )
            session.start()
            await session.send_control(
                "RESUME_ACCEPT",
                {
                    "host": service.username or "Host",
                    "room_id": room.room_id,
                    "session_id": room.session_id,
                    "participant_id": participant_id,
                    "resume_token": next_token,
                },
            )
            console.print(f"[green]✓ {peer_name} reconnected.[/green]")
            return session
        except (asyncio.TimeoutError, BlueChatError, ConnectionError, OSError, ValueError) as exc:
            if connection is not None:
                await connection.close()
            if time.monotonic() >= deadline:
                break
            if not isinstance(exc, asyncio.TimeoutError):
                logging.getLogger(__name__).debug(
                    "Rejected a private resume attempt (%s)", type(exc).__name__
                )
            await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return None


async def _expire_reconnecting_participant(
    room: Room,
    router: GroupRouter,
    history: HistoryManager,
    history_path: Path | None,
    reconnect_deadlines: dict[str, float],
    resume_tokens: dict[str, str],
    participant_id: str,
    name: str,
    deadline: float,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Remove a suspended member only if their original deadline still applies."""
    await sleep(max(0.0, deadline - clock()))
    if reconnect_deadlines.get(participant_id) != deadline:
        return False
    reconnect_deadlines.pop(participant_id, None)
    resume_tokens.pop(participant_id, None)
    room.remove_participant(participant_id)
    history.append(history_path, "left", sender=name)
    await router.broadcast_event("USER_LEFT", {"participant_id": participant_id, "username": name})
    return True


async def _accept_group_members(
    service: BlueChat,
    room: Room,
    router: GroupRouter,
    transport: BluetoothTransport,
    history: HistoryManager | None = None,
    history_path: Path | None = None,
    resume_registry: ResumeRegistry | None = None,
    resume_tokens: dict[str, str] | None = None,
    reconnect_deadlines: dict[str, float] | None = None,
    reconnect_tasks: dict[str, asyncio.Task[None]] | None = None,
    restart_event: asyncio.Event | None = None,
    room_ended: asyncio.Event | None = None,
) -> None:
    resume_registry = resume_registry or ResumeRegistry()
    resume_tokens = resume_tokens if resume_tokens is not None else {}
    reconnect_deadlines = reconnect_deadlines if reconnect_deadlines is not None else {}
    reconnect_tasks = reconnect_tasks if reconnect_tasks is not None else {}
    while room_ended is None or not room_ended.is_set():
        connection: Connection | None = None
        session: AsyncChatSession | None = None
        try:
            connection = await _accept_after_host_restart(transport, restart_event, room_ended)
            initial = await asyncio.wait_for(connection.receive(), timeout=10.0)
            if initial.startswith(b"BC-RC1"):
                keys, resumed_room, participant_id = await asyncio.wait_for(
                    host_resume(
                        connection,
                        resume_registry,
                        room.session_id,
                        lambda room_id, pid: (
                            resume_tokens.get(pid) if room_id == room.room_id else None
                        ),
                        first_message=initial,
                    ),
                    timeout=15.0,
                )
                deadline = reconnect_deadlines.get(participant_id, 0.0)
                name = room.participant_names.get(participant_id)
                if resumed_room != room.room_id or deadline < time.monotonic() or name is None:
                    raise AuthenticationError("The reconnect window has expired")
                previous = reconnect_tasks.pop(participant_id, None)
                if previous is not None:
                    previous.cancel()
                reconnect_deadlines.pop(participant_id, None)
                token = resume_registry.issue(
                    room.room_id, participant_id, room.session_id, deferred_expiry=True
                )
                resume_tokens[participant_id] = token
                session = AsyncChatSession(
                    service.username or "Host",
                    name,
                    connection,
                    keys,
                    participant_id=participant_id,
                    room_id=room.room_id,
                )
                session.start()
                await session.send_control(
                    "RESUME_ACCEPT",
                    {
                        "host": service.username or "Host",
                        "room_id": room.room_id,
                        "session_id": room.session_id,
                        "participant_id": participant_id,
                        "resume_token": token,
                    },
                )
                await router.add(participant_id, name, session)
                await router.broadcast_event(
                    "USER_RECONNECTED", {"participant_id": participant_id, "username": name}
                )
                console.print(f"[green]✓ {name} reconnected.[/green]")
                continue
            if initial != b"BC-INIT":
                raise AuthenticationError("Peer did not begin the BlueChat handshake")
            room.check_auth_rate_limit()
            keys = await asyncio.wait_for(
                perform_host_handshake(connection, room.code), timeout=30.0
            )
            room.record_auth_success()
            session = AsyncChatSession(
                service.username or "Host", None, connection, keys, room_id=room.room_id
            )
            session.start()
            hello = await session.receive_control("HELLO", timeout=15.0)
            guest = _validated_payload_username(hello.payload, "username")
            participant_id = room.request_join(
                guest,
                room.code,
                approved=typer.confirm(f"{guest} wants to join. Accept?", default=False),
            )
            session.peer_username = guest
            session.participant_id = participant_id
            resume_token = resume_registry.issue(
                room.room_id, participant_id, room.session_id, deferred_expiry=True
            )
            resume_tokens[participant_id] = resume_token
            await session.send_control(
                "JOIN_ACCEPT",
                {
                    "host": service.username or "Host",
                    "room_id": room.room_id,
                    "session_id": room.session_id,
                    "participant_id": participant_id,
                    "group": True,
                    "resume_token": resume_token,
                    "participants": [
                        {"participant_id": pid, "username": name}
                        for pid, name in room.list_participants()
                    ],
                },
            )
            await router.add(participant_id, guest, session)
            await router.broadcast_event(
                "USER_JOINED", {"participant_id": participant_id, "username": guest}
            )
            if history is not None:
                history.append(history_path, "joined", sender=guest)
            console.print(f"[green]{guest} joined the room.[/green]")
        except asyncio.TimeoutError:
            if session is not None:
                await session.close()
            elif connection is not None:
                await connection.close()
            continue
        except (BlueChatError, AuthenticationError, ValueError) as exc:
            if isinstance(exc, AuthenticationError) and not isinstance(
                exc, AuthenticationRateLimitedError
            ):
                room.record_auth_failure()
            console.print(f"[yellow]Join attempt failed:[/yellow] {exc}")
            if session is not None:
                await session.close()
            elif connection is not None:
                await connection.close()
        except (ConnectionError, OSError):
            if session is not None:
                await session.close()
            elif connection is not None:
                await connection.close()
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Could not process a BlueChat participant connection (%s)", type(exc).__name__
            )
            if session is not None:
                await session.close()
            elif connection is not None:
                await connection.close()
        except asyncio.CancelledError:
            if session is not None:
                await session.close()
            elif connection is not None:
                await connection.close()
            raise


async def _group_host_prompt(
    service: BlueChat,
    room: Room,
    router: GroupRouter,
    accept_task: asyncio.Task[None],
    history: HistoryManager | None = None,
    history_path: Path | None = None,
    reconnect_deadlines: dict[str, float] | None = None,
    room_ended: asyncio.Event | None = None,
) -> None:
    prompt: PromptSession[str] = PromptSession()
    incoming_task = asyncio.create_task(_display_group_incoming(router, history, history_path))
    countdown_task = asyncio.create_task(_display_group_countdown(room))
    ended_task = asyncio.create_task(room_ended.wait()) if room_ended is not None else None
    send_tasks: set[asyncio.Task[None]] = set()
    commands = "Commands: /help /clear /info /who /newcode /send <path> /disconnect /quit"
    try:
        with patch_stdout():
            print(commands)
            while True:
                try:
                    input_task = asyncio.create_task(prompt.prompt_async("You > "))
                    if ended_task is None:
                        line = await input_task
                    else:
                        done, _ = await asyncio.wait(
                            (input_task, ended_task), return_when=asyncio.FIRST_COMPLETED
                        )
                        if ended_task in done:
                            input_task.cancel()
                            await asyncio.gather(input_task, return_exceptions=True)
                            print(
                                "This BlueChat room has ended because the host service could not recover."
                            )
                            break
                        line = input_task.result()
                except (EOFError, KeyboardInterrupt):
                    break
                value = line.strip()
                if not value:
                    continue
                if value in {"/disconnect", "/quit"}:
                    await router.broadcast_event("DISCONNECT", {"reason": "Host closed the room"})
                    break
                if value == "/help":
                    print(commands)
                elif value == "/clear":
                    print("\033[2J\033[H", end="")
                elif value == "/who":
                    reconnecting_ids = {
                        participant_id
                        for participant_id, deadline in (reconnect_deadlines or {}).items()
                        if deadline > time.monotonic()
                    }
                    print(
                        _format_who(
                            dict(room.list_participants()),
                            reconnecting=reconnecting_ids,
                        )
                    )
                elif value == "/info":
                    print(
                        _format_session_info(
                            "Group",
                            service.username or "Host",
                            len(room.participants),
                            room.session_started_at,
                            history_path is not None,
                        )
                    )
                elif value == "/newcode":
                    print(f"New room code: {_rotate_join_code(room)} (expires in 05:00)")
                elif value.startswith("/send"):
                    try:
                        path = Path(_parse_send_path(value))
                    except ValueError as exc:
                        print(exc)
                        continue
                    task = asyncio.create_task(
                        _send_group_file_from_host(router, path, history, history_path),
                        name="bluechat-group-file-send",
                    )
                    send_tasks.add(task)
                    task.add_done_callback(send_tasks.discard)
                elif value.startswith("/"):
                    print("Unknown command. Type /help for commands.")
                else:
                    message = ChatMessage(
                        service.username or "Host",
                        value,
                        secrets.token_urlsafe(16),
                        "host",
                        room.room_id,
                        "",
                    )
                    await router.publish(message)
                    if history is not None:
                        history.append(history_path, "message", sender=message.sender, text=value)
    finally:
        if not accept_task.done():
            accept_task.cancel()
        await asyncio.gather(accept_task, return_exceptions=True)
        incoming_task.cancel()
        countdown_task.cancel()
        if ended_task is not None:
            ended_task.cancel()
        for task in send_tasks:
            task.cancel()
        await asyncio.gather(
            incoming_task,
            countdown_task,
            *(task for task in (ended_task,) if task is not None),
            return_exceptions=True,
        )
        await asyncio.gather(*send_tasks, return_exceptions=True)


async def _send_group_file_from_host(
    router: GroupRouter,
    path: Path,
    history: HistoryManager | None = None,
    history_path: Path | None = None,
) -> None:
    """Offer a host-selected file independently to each connected member."""
    manager = FileTransferManager()
    peers = tuple(router.peer_sessions.items())
    if not peers:
        print("No participants are connected to receive the file.")
        return

    async def send_one(name: str, session: AsyncChatSession) -> tuple[str, str]:
        progress_state = [-5]

        def progress(done: int, total: int) -> None:
            percent = int(done * 100 / total) if total else 100
            if percent >= progress_state[0] + 5 or percent == 100:
                progress_state[0] = percent
                print(f"\rSending {path.name} to {name}: {percent}%", end="", flush=True)

        try:
            await manager.send_file(session, path, progress=progress)
            return name, "accepted"
        except TransferError as exc:
            return name, str(exc)
        except (ConnectionError, OSError) as exc:
            return name, f"transfer failed ({type(exc).__name__})"

    names = {participant_id: name for participant_id, name in router.participants}
    results = await asyncio.gather(
        *(send_one(names.get(peer_id, "Participant"), session) for peer_id, session in peers)
    )
    for name, result in results:
        if result == "accepted":
            print(f"\n✓ Sent {path.name} to {name}.")
            if history is not None:
                history.append(
                    history_path,
                    "file",
                    sender="You",
                    metadata={"filename": path.name, "recipient": name, "event": "sent"},
                )
        else:
            print(f"\n{name} did not receive {path.name}: {result}")


async def _display_group_incoming(
    router: GroupRouter,
    history: HistoryManager | None = None,
    history_path: Path | None = None,
) -> None:
    while True:
        message = await router.incoming.get()
        print(f"\n{message.sender} > {message.text}")
        if history is not None:
            history.append(history_path, "message", sender=message.sender, text=message.text)


async def _display_group_countdown(room: Room) -> None:
    while True:
        print(
            f"\rRoom code expires in {format_countdown(room.remaining_seconds())}  ",
            end="",
            flush=True,
        )
        await asyncio.sleep(1)


async def _wait_for_room_peer(
    room: Room,
    transport: BluetoothTransport,
    restart_event: asyncio.Event | None = None,
    room_ended: asyncio.Event | None = None,
) -> Connection | None:
    accept_task = asyncio.create_task(
        _accept_after_host_restart(transport, restart_event, room_ended)
    )
    try:
        with Live(console=console, refresh_per_second=2, transient=False) as live:
            while room.remaining_seconds() > 0:
                remaining = room.remaining_seconds()
                live.update(_room_panel(room, remaining))
                done, _ = await asyncio.wait({accept_task}, timeout=min(1.0, float(remaining)))
                if done:
                    return accept_task.result()
            return None
    finally:
        if not accept_task.done():
            accept_task.cancel()
            try:
                await accept_task
            except asyncio.CancelledError:
                pass


async def _accept_after_host_restart(
    transport: BluetoothTransport,
    restart_event: asyncio.Event | None,
    room_ended: asyncio.Event | None = None,
) -> Connection:
    """Wait for a connection, replacing an accept blocked on a restarted server."""
    if restart_event is None and room_ended is not None:
        restart_event = asyncio.Event()
    while True:
        if restart_event is None:
            return await transport.accept()
        accept_task = asyncio.create_task(transport.accept())
        restart_task = asyncio.create_task(restart_event.wait())
        ended_task = asyncio.create_task(room_ended.wait()) if room_ended is not None else None
        watchers = (accept_task, restart_task) + ((ended_task,) if ended_task else ())
        try:
            done, _ = await asyncio.wait(watchers, return_when=asyncio.FIRST_COMPLETED)
        finally:
            pending = [task for task in watchers if not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        if ended_task is not None and ended_task in done:
            raise BluetoothUnavailableError("The host Bluetooth service could not recover")
        if accept_task in done:
            if restart_event.is_set():
                restart_event.clear()
            return accept_task.result()
        if restart_task in done:
            restart_event.clear()
            if not accept_task.done():
                accept_task.cancel()
                await asyncio.gather(accept_task, return_exceptions=True)
            continue
        return accept_task.result()


async def _monitor_host_service(
    service: BlueChat,
    room: Room,
    transport: BluetoothTransport,
    restart_event: asyncio.Event,
    room_ended: asyncio.Event,
    *,
    window_seconds: float = _HOST_RECOVERY_WINDOW,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Restart an interrupted native host service while preserving room state."""
    metadata = {"device_name": service.username or "Host", "room_id": room.room_id}
    while not room_ended.is_set():
        try:
            await transport.wait_host_failure()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Host service monitoring failed (%s)", type(exc).__name__
            )
            room_ended.set()
            return
        console.print(
            "[yellow]Host Bluetooth service lost. Trying to restore the room for 30 seconds.[/yellow]"
        )
        try:
            await transport.stop_advertising()
            await transport.stop_server()
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "Host transport cleanup during recovery failed (%s)", type(exc).__name__
            )
        deadline = clock() + window_seconds
        restored = False
        while clock() < deadline:
            remaining = deadline - clock()
            try:
                available = await asyncio.wait_for(
                    transport.is_available(), timeout=min(_HOST_PROBE_TIMEOUT, remaining)
                )
                enabled = available and await asyncio.wait_for(
                    transport.is_enabled(), timeout=min(_HOST_PROBE_TIMEOUT, remaining)
                )
                if enabled:
                    await transport.start_server()
                    await transport.advertise(metadata)
                    restored = True
                    break
            except (BlueChatError, asyncio.TimeoutError, OSError, RuntimeError) as exc:
                logging.getLogger(__name__).debug(
                    "Host service recovery attempt failed (%s)", type(exc).__name__
                )
                try:
                    await transport.stop_server()
                except Exception as cleanup_error:
                    logging.getLogger(__name__).debug(
                        "Host transport cleanup failed (%s)", type(cleanup_error).__name__
                    )
            await sleep(min(_HOST_RECOVERY_POLL, max(0.0, deadline - clock())))
        if not restored:
            room_ended.set()
            console.print(
                "[yellow]Host could not restore Bluetooth within 30 seconds. The room has ended.[/yellow]"
            )
            return
        restart_event.set()
        console.print("[green]✓ Host Bluetooth service restored. Room state retained.[/green]")


@app.command()
def join(
    device: Optional[str] = typer.Option(None, "--device", help="Select a discovered device."),
) -> None:
    """Find and join a nearby BlueChat room."""
    asyncio.run(_join(_service(), device=device))


async def _join(service: BlueChat, *, device: str | None = None) -> None:
    try:
        await service.bluetooth.require_ready(role="client")
        devices = await service.bluetooth.transport.discover()
        if not devices:
            console.print("[yellow]No nearby Bluetooth devices were found.[/yellow]")
            return
        table = Table("#", "Device", "Status")
        for index, item in enumerate(devices, 1):
            table.add_row(
                str(index), item.name, "BlueChat Host" if item.bluechat_host else "Bluetooth Device"
            )
        console.print(table)
        if device:
            matches = [item for item in devices if device in {item.identifier, item.name}]
            if not matches:
                console.print(f"[red]No discovered device matches {device!r}.[/red]")
                return
            selected_device = matches[0]
        else:
            selected = typer.prompt("Select device number")
            if not selected.isdecimal() or not 1 <= int(selected) <= len(devices):
                console.print("[red]Select one of the listed device numbers.[/red]")
                return
            selected_device = devices[int(selected) - 1]
        if not selected_device.bluechat_host:
            console.print(
                "[yellow]This device did not advertise BlueChat; attempting service discovery anyway.[/yellow]"
            )
        connection = await service.bluetooth.transport.connect(selected_device)
        try:
            code = validate_room_code(typer.prompt("Enter BlueChat room code").strip())
            await connection.send(b"BC-INIT")
            keys = await asyncio.wait_for(perform_client_handshake(connection, code), timeout=30.0)
            host_name = selected_device.name
            session = AsyncChatSession(service.username or "Guest", host_name, connection, keys)
            session.start()
            await session.send_control("HELLO", {"username": service.username or "Guest"})
            response = await session.receive_control(timeout=300.0)
            if response.type == "JOIN_REJECT":
                console.print("[yellow]The host declined the request.[/yellow]")
                await session.close()
                return
            if response.type != "JOIN_ACCEPT":
                raise AuthenticationError("Unexpected response while waiting for host approval")
            session.peer_username = _validated_payload_username(response.payload, "host")
            is_group = response.payload.get("group") is True
            session.room_id = str(response.payload.get("room_id", ""))
            session_id = str(response.payload.get("session_id", ""))
            session.participant_id = str(
                response.payload.get("participant_id", session.participant_id)
            )
            session.allow_remote_senders = is_group
            resume_token = response.payload.get("resume_token")
            resume_context = None
            if isinstance(resume_token, str) and session_id:
                resume_context = {
                    "device": selected_device,
                    "room_id": session.room_id,
                    "session_id": session_id,
                    "participant_id": session.participant_id,
                    "resume_token": resume_token,
                    "username": service.username or "Guest",
                    "host": session.peer_username,
                    "group": is_group,
                }
            roster: dict[str, str] = {}
            participants = response.payload.get("participants", [])
            if isinstance(participants, list):
                for item in participants:
                    if isinstance(item, dict) and isinstance(item.get("username"), str):
                        try:
                            name = validate_username(item["username"])
                        except ValueError:
                            continue
                        roster[str(item.get("participant_id", name))] = name
            if not roster:
                roster = {
                    "host": session.peer_username,
                    session.participant_id: service.username or "Guest",
                }
            label = "group" if is_group else "private"
            console.print(
                f"[green]Connected to {session.peer_username}. Encrypted {label} chat is ready.[/green]"
            )
            await _chat_loop(
                session,
                service.username or "Guest",
                session.peer_username,
                "Group" if is_group else "Private",
                service=service,
                room_id=session.room_id,
                roster=roster,
                host_name=session.peer_username,
                resume_context=resume_context,
            )
        finally:
            await connection.close()
    except (BlueChatError, AuthenticationError, ValueError) as exc:
        _friendly_error(exc if isinstance(exc, BlueChatError) else ProtocolError(str(exc)))
    except asyncio.TimeoutError:
        console.print("[yellow]The host did not respond before the connection timed out.[/yellow]")
    except typer.Abort:
        console.print("\n[dim]Joining cancelled.[/dim]")
    except Exception as exc:
        _unexpected_error(exc)


@app.command()
def devices(
    details: bool = typer.Option(False, "--details", help="Show transport details."),
) -> None:
    """Scan for nearby Bluetooth devices."""
    service = _service()
    try:
        found = asyncio.run(_discover_devices(service))
    except BlueChatError as exc:
        _friendly_error(exc)
        return
    except Exception as exc:
        _unexpected_error(exc)
    if not found:
        console.print("No nearby devices found.")
        return
    table = Table("#", "Device", "Status")
    for index, item in enumerate(found, 1):
        status = (
            "BlueChat Host"
            if item.bluechat_host
            else "Paired"
            if item.paired
            else "Bluetooth Device"
        )
        table.add_row(str(index), item.name, status)
        if details:
            console.print(
                f"{item.name}\nAddress: {item.address or 'Unavailable'}\nDetails: {item.details or '—'}"
            )
    console.print(table)


async def _discover_devices(service: BlueChat):
    await service.bluetooth.require_ready(role="client")
    return await service.bluetooth.transport.discover()


async def _chat_loop(
    session: AsyncChatSession,
    username: str,
    peer: str,
    room_type: str,
    *,
    service: BlueChat | None = None,
    room_id: str = "",
    roster: dict[str, str] | None = None,
    host_name: str | None = None,
    resume_context: dict[str, object] | None = None,
) -> str:
    session.start()
    prompt: PromptSession[str] = PromptSession()
    participant_names = roster or {username: username, peer: peer}
    reconnecting: set[str] = set()
    started_at = time.monotonic()
    history = HistoryManager()
    history_path = None
    if service is not None:
        consent = None
        if service.config.history is HistoryPreference.ASK:
            consent = typer.confirm("Save this conversation locally?", default=False)
        try:
            history_path = history.create(
                peer,
                room_id=room_id or "local",
                preference=service.config.history,
                consent=consent,
            )
        except OSError as exc:
            console.print(f"[yellow]Local history is unavailable ({type(exc).__name__}).[/yellow]")
    transfers = FileTransferManager()
    send_tasks: set[asyncio.Task[None]] = set()
    receive_task = asyncio.create_task(_display_incoming(session, history, history_path))
    end_reason = "local_disconnect"
    commands = "Commands: /help /clear /info /who /disconnect /quit /send <path>"
    try:
        with patch_stdout():
            print(commands)
            while not receive_task.done():
                input_task = asyncio.create_task(prompt.prompt_async("You > "))
                control_task = asyncio.create_task(session.receive_control())
                try:
                    done, _ = await asyncio.wait(
                        {input_task, control_task, receive_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if receive_task in done:
                        for pending_task in (input_task, control_task):
                            if not pending_task.done():
                                pending_task.cancel()
                        await asyncio.gather(input_task, control_task, return_exceptions=True)
                        end_reason = "transport_lost"
                        if resume_context is None:
                            break
                        resumed = await _reconnect_client(service, resume_context)
                        if resumed is None:
                            print(
                                "The host did not reconnect within 30 seconds; this room has ended."
                            )
                            break
                        session = resumed
                        end_reason = "active"
                        peer = session.peer_username or peer
                        receive_task = asyncio.create_task(
                            _display_incoming(session, history, history_path)
                        )
                        print(f"✓ Reconnected to {peer}.")
                        continue
                    if control_task in done:
                        try:
                            control = control_task.result()
                        except ConnectionError:
                            print("\n[BlueChat] Connection closed.")
                            end_reason = "transport_lost"
                            if resume_context is None:
                                break
                            resumed = await _reconnect_client(service, resume_context)
                            if resumed is None:
                                print(
                                    "The host did not reconnect within 30 seconds; this room has ended."
                                )
                                break
                            session = resumed
                            end_reason = "active"
                            peer = session.peer_username or peer
                            receive_task = asyncio.create_task(
                                _display_incoming(session, history, history_path)
                            )
                            print(f"✓ Reconnected to {peer}.")
                            continue
                        if not input_task.done():
                            input_task.cancel()
                            await asyncio.gather(input_task, return_exceptions=True)
                        if control.type == "FILE_OFFER":
                            try:
                                received = await _receive_file_offer(
                                    prompt, session, control, transfers, service
                                )
                                if received:
                                    print(f"✓ File received and verified: {received}")
                                    history.append(
                                        history_path,
                                        "file",
                                        sender=peer,
                                        metadata={"filename": received.name},
                                    )
                            except TransferError as exc:
                                print(f"Transfer failed: {exc}")
                        elif control.type == "USER_JOINED":
                            name = control.payload.get("username")
                            identity = control.payload.get("participant_id")
                            if isinstance(name, str) and isinstance(identity, str):
                                try:
                                    participant_names[identity] = validate_username(name)
                                except ValueError:
                                    pass
                                else:
                                    print(f"\n{name} joined the room.")
                                    history.append(history_path, "joined", sender=name)
                        elif control.type == "USER_LEFT":
                            identity = control.payload.get("participant_id")
                            if not isinstance(identity, str):
                                identity = ""
                            name = participant_names.pop(
                                identity, control.payload.get("username", "A participant")
                            )
                            print(f"\n{name} left the room.")
                            history.append(history_path, "left", sender=str(name))
                        elif control.type == "USER_DISCONNECTED":
                            identity = control.payload.get("participant_id")
                            name = control.payload.get("username", "A participant")
                            if isinstance(identity, str):
                                reconnecting.add(identity)
                            print(f"\n{name} disconnected; waiting for reconnection.")
                        elif control.type == "USER_RECONNECTED":
                            identity = control.payload.get("participant_id")
                            name = control.payload.get("username", "A participant")
                            if isinstance(identity, str):
                                reconnecting.discard(identity)
                            print(f"\n✓ {name} reconnected.")
                        elif control.type == "DISCONNECT":
                            print("\nThe host ended the room.")
                            end_reason = "remote_disconnect"
                            break
                        else:
                            print(f"\nBlueChat event: {control.type}")
                        continue
                    control_task.cancel()
                    await asyncio.gather(control_task, return_exceptions=True)
                    try:
                        line = input_task.result()
                    except (EOFError, KeyboardInterrupt, asyncio.CancelledError):
                        break
                except (EOFError, KeyboardInterrupt):
                    break
                finally:
                    for task in (input_task, control_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(input_task, control_task, return_exceptions=True)
                value = line.strip()
                if not value:
                    continue
                if value in {"/disconnect", "/quit"}:
                    await session.send_control("DISCONNECT", {})
                    end_reason = "local_disconnect"
                    break
                if value == "/help":
                    print(commands)
                elif value == "/clear":
                    print("\033[2J\033[H", end="")
                elif value == "/info":
                    print(
                        _format_session_info(
                            room_type,
                            host_name or peer,
                            len(participant_names),
                            started_at,
                            history_path is not None,
                        )
                    )
                elif value == "/who":
                    print(_format_who(participant_names, reconnecting=reconnecting))
                elif value.startswith("/send"):
                    try:
                        path = Path(_parse_send_path(value))
                    except ValueError as exc:
                        print(exc)
                        continue
                    last_progress = [-5]

                    def send_progress(
                        done: int,
                        total: int,
                        *,
                        current_path: Path = path,
                        progress_state: list[int] = last_progress,
                    ) -> None:
                        percent = int(done * 100 / total) if total else 100
                        if percent >= progress_state[0] + 5 or percent == 100:
                            progress_state[0] = percent
                            print(
                                f"\rSending {current_path.name}: {percent}%",
                                end="",
                                flush=True,
                            )

                    async def send_file(
                        current_path: Path = path,
                        current_session: AsyncChatSession = session,
                    ) -> None:
                        try:
                            await transfers.send_file(
                                current_session, current_path, progress=send_progress
                            )
                            print(f"\n✓ Sent {current_path.name} successfully.")
                            history.append(
                                history_path,
                                "file",
                                sender=username,
                                metadata={"filename": current_path.name},
                            )
                        except (TransferError, OSError) as exc:
                            print(f"\nFile was not sent: {exc}")

                    send_task = asyncio.create_task(send_file(), name="bluechat-file-send")
                    send_tasks.add(send_task)
                    send_task.add_done_callback(send_tasks.discard)
                elif value.startswith("/"):
                    print("Unknown command. Type /help for commands.")
                else:
                    try:
                        await session.send_text(value)
                        history.append(history_path, "message", sender=username, text=value)
                    except ValueError as exc:
                        print(f"Message not sent: {exc}")
    finally:
        receive_task.cancel()
        for send_job in send_tasks:
            send_job.cancel()
        try:
            await receive_task
        except asyncio.CancelledError:
            pass
        await asyncio.gather(*send_tasks, return_exceptions=True)
        await session.close()
    return end_reason


async def _display_incoming(
    session: AsyncChatSession, history: HistoryManager, history_path: Path | None
) -> None:
    while True:
        try:
            message = await session.receive()
        except ConnectionError:
            print("\n[BlueChat] Connection closed.")
            return
        print(f"\n{message.sender} > {message.text}")
        history.append(history_path, "message", sender=message.sender, text=message.text)


async def _reconnect_client(
    service: BlueChat | None,
    context: dict[str, object],
    *,
    window_seconds: float = 30.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncChatSession | None:
    """Retry a saved group peer for at most 30 seconds using secure resume."""
    if service is None:
        return None
    device = context.get("device")
    token = context.get("resume_token")
    room_id = context.get("room_id")
    session_id = context.get("session_id")
    participant_id = context.get("participant_id")
    username = context.get("username")
    host = context.get("host")
    group = context.get("group") is True
    if not isinstance(device, BluetoothDevice) or not all(
        isinstance(value, str)
        for value in (token, room_id, session_id, participant_id, username, host)
    ):
        return None
    assert isinstance(token, str)
    assert isinstance(room_id, str)
    assert isinstance(session_id, str)
    assert isinstance(participant_id, str)
    assert isinstance(username, str)
    assert isinstance(host, str)
    deadline = clock() + window_seconds
    print("Host connection lost. Trying secure session resumption.")
    delays = (0.0, 0.5, 1.0, 2.0, 4.0, 5.0)
    attempt = 0
    while clock() < deadline:
        remaining = deadline - clock()
        print(
            f"\rAttempting to reconnect... {max(0, int(remaining))} seconds remaining",
            end="",
            flush=True,
        )
        connection: Connection | None = None
        session: AsyncChatSession | None = None
        try:
            connection = await asyncio.wait_for(
                service.bluetooth.transport.connect(device), timeout=min(8.0, remaining)
            )
            assert connection is not None
            keys = await asyncio.wait_for(
                client_resume(connection, token, room_id, session_id, participant_id),
                timeout=min(8.0, remaining),
            )
            session = AsyncChatSession(
                username,
                host,
                connection,
                keys,
                participant_id=participant_id,
                room_id=room_id,
                allow_remote_senders=group,
            )
            session.start()
            response = await session.receive_control("RESUME_ACCEPT", timeout=min(8.0, remaining))
            if (
                response.payload.get("room_id") != room_id
                or response.payload.get("session_id") != session_id
                or response.payload.get("participant_id") != participant_id
                or not isinstance(response.payload.get("resume_token"), str)
            ):
                raise AuthenticationError("The host returned an invalid resume confirmation")
            context["resume_token"] = response.payload["resume_token"]
            print("\n")
            return session
        except (BlueChatError, asyncio.TimeoutError, ConnectionError, OSError, ValueError):
            if session is not None:
                await session.close()
            elif connection is not None:
                await connection.close()
            delay = delays[min(attempt, len(delays) - 1)]
            attempt += 1
            await sleep(min(delay, max(0, deadline - clock())))
    print()
    return None


def _format_session_info(
    room_type: str,
    host: str,
    participant_count: int,
    started_at: float,
    history_enabled: bool,
) -> str:
    """Format a user-safe summary without room codes or cryptographic state."""
    elapsed = max(0, int(time.monotonic() - started_at))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    capacity = 5 if room_type.casefold() == "group" else 2
    return (
        "Session Information\n"
        f"Room type: {room_type}\n"
        f"Host: {host}\n"
        f"Participants: {participant_count}/{capacity}\n"
        "Transport: Bluetooth LE\n"
        "Encryption: Active\n"
        "Protocol: v1\n"
        f"Session duration: {hours:02}:{minutes:02}:{seconds:02}\n"
        f"History: {'Enabled locally' if history_enabled else 'Disabled locally'}"
    )


def _validated_payload_username(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise AuthenticationError("The peer sent an invalid username")
    return validate_username(value)


def _format_who(participants: dict[str, str], *, reconnecting: set[str] | None = None) -> str:
    """Render a participant roster without exposing protocol identifiers."""
    reconnecting = reconnecting or set()
    lines = ["Participants"]
    for identity, name in participants.items():
        role = " (Host)" if identity == "host" else ""
        state = " — reconnecting" if identity in reconnecting else ""
        lines.append(f"{name}{role}{state}")
    return "\n".join(lines)


def _rotate_join_code(room: Room) -> str:
    """Rotate the host's joining code without changing active participants."""
    return room.rotate_code()


def _parse_send_path(command: str) -> str:
    """Parse one quoted path while preserving Windows backslashes and spaces."""
    raw = command[len("/send") :].strip()
    if not raw:
        raise ValueError("Usage: /send <path>")
    if raw[0] in {"'", '"'}:
        quote = raw[0]
        end = raw.find(quote, 1)
        if end < 0 or raw[end + 1 :].strip():
            raise ValueError('Quote the path once, for example /send "my file.pdf"')
        return raw[1:end]
    # Unquoted paths are accepted whole, including spaces.
    return raw


async def _receive_file_offer(
    prompt: PromptSession[str],
    session: AsyncChatSession,
    offer,
    manager: FileTransferManager,
    service: BlueChat | None,
) -> Path | None:
    payload = offer.payload
    filename = payload.get("filename")
    size = payload.get("size")
    mime = payload.get("mime_type")
    if not isinstance(filename, str) or not isinstance(size, int) or not isinstance(mime, str):
        raise TransferError("The incoming file offer is invalid")
    filename = safe_filename(filename)
    print(f"\nFile Transfer Request\nFile: {filename}\nSize: {size:,} bytes\nType: {mime}")
    answer = await prompt.prompt_async("Accept? [y/N] ")
    if answer.strip().lower() not in {"y", "yes"}:
        return await manager.receive_file(session, offer, _deny_file)
    root = (
        service.config.download_dir
        if service and service.config.download_dir
        else Path.home() / "BlueChat" / "Downloads"
    )
    category = file_category(filename, mime)
    default_folder = root / category
    destination_choice = await prompt.prompt_async(
        f"Save to default {default_folder}? [Y/n/custom path] "
    )
    if destination_choice.strip().lower() in {"n", "no"}:
        custom = await prompt.prompt_async("Choose destination folder: ")
        folder = Path(custom).expanduser()
    elif destination_choice.strip() and destination_choice.strip().lower() not in {"y", "yes"}:
        folder = Path(destination_choice).expanduser()
    else:
        folder = default_folder

    async def approve(_name: str, _size: int, _mime: str) -> Path:
        return folder

    last_progress = [-5]

    def receive_progress(done: int, total: int) -> None:
        percent = int(done * 100 / total) if total else 100
        if percent >= last_progress[0] + 5 or percent == 100:
            last_progress[0] = percent
            print(f"\rReceiving {filename}: {percent}%", end="", flush=True)

    return await manager.receive_file(session, offer, approve, progress=receive_progress)


async def _deny_file(_name: str, _size: int, _mime: str) -> None:
    return None


@app.command()
def config(setting: Optional[str] = typer.Argument(None)) -> None:
    """View or change local settings."""
    service = _service()
    if setting == "username":
        try:
            service.set_username(
                validate_username(typer.prompt("Username", default=service.username or ""))
            )
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(2) from exc
    elif setting == "downloads":
        path = Path(
            typer.prompt("Download directory", default=str(service.config.download_dir or ""))
        ).expanduser()
        service.set_download_dir(path)
    elif setting == "history":
        current = service.config.history.value
        answer = typer.prompt("History: ask, always, or never", default=current).strip().lower()
        try:
            service.update_config(history=HistoryPreference(answer))
        except ValueError as exc:
            console.print("[red]Choose ask, always, or never.[/red]")
            raise typer.Exit(2) from exc
    elif setting == "debug":
        service.update_config(debug_logging=not service.config.debug_logging)
    elif setting not in (None,):
        console.print("Settings: username, history, downloads, debug")
        raise typer.Exit(2)
    console.print(f"Username: {service.username or 'Not set'}")
    console.print(f"History: {service.config.history.value}")
    console.print(f"Downloads: {service.config.download_dir}")
    console.print(f"Config file: {service.config_manager.path}")


@app.command()
def info() -> None:
    """Show version, platform, and implementation status."""
    service = _service()
    capabilities = service.bluetooth.transport.capabilities
    host_status = (
        "GATT host available" if capabilities.hosting else "client only; host backend pending"
    )
    console.print(
        Panel(
            f"BlueChat {__version__}\nPython {platform.python_version()}\n"
            f"Platform: {platform.system()}\nBLE: discovery={capabilities.discovery}, {host_status}",
            title="About BlueChat",
        )
    )


@app.command()
def doctor() -> None:
    """Show backend, Bluetooth, and local security diagnostics."""
    try:
        service = BlueChat()
        asyncio.run(_doctor(service))
    except BlueChatError as exc:
        _friendly_error(exc)
    except Exception as exc:
        _unexpected_error(exc)


async def _doctor(service: BlueChat) -> None:
    transport = service.bluetooth.transport
    try:
        available = await asyncio.wait_for(transport.is_available(), timeout=_DOCTOR_PROBE_TIMEOUT)
    except Exception as exc:
        available = False
        available_detail = f"Error: {type(exc).__name__}"
    else:
        available_detail = "Available" if available else "Unavailable or permission denied"
    try:
        enabled = (
            await asyncio.wait_for(transport.is_enabled(), timeout=_DOCTOR_PROBE_TIMEOUT)
            if available
            else False
        )
    except Exception as exc:
        enabled = False
        enabled_detail = f"Error: {type(exc).__name__}"
    else:
        enabled_detail = "On" if enabled else "Off or permission denied"

    table = Table("Check", "Status", "Details", title="BlueChat Diagnostics")
    table.add_row("Platform", "Info", f"{platform.system()} ({platform.platform()})")
    table.add_row("Python", "Info", platform.python_version())
    table.add_row("Backend", "Info", type(transport).__name__)
    table.add_row("Bluetooth adapter", "✓" if available else "!", available_detail)
    table.add_row("Bluetooth state", "✓" if enabled else "!", enabled_detail)
    capabilities = transport.capabilities
    table.add_row(
        "Discovery",
        "✓" if capabilities.discovery else "—",
        "Supported" if capabilities.discovery else "Unsupported",
    )
    table.add_row(
        "Hosting",
        "✓" if capabilities.hosting else "—",
        "Supported" if capabilities.hosting else "Unsupported by this backend",
    )
    table.add_row(
        "Advertising",
        "✓" if capabilities.advertising else "—",
        "Supported" if capabilities.advertising else "Unsupported by this backend",
    )
    table.add_row("Encryption", "✓", "cryptography / AES-GCM")
    table.add_row(
        "Username",
        "✓" if service.username else "!",
        service.username or "Not configured; launch bluechat to choose one",
    )
    console.print(table)


@app.command()
def version() -> None:
    """Show the installed BlueChat version."""
    console.print(__version__)
