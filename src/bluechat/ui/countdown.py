"""Live display for the five-minute room-code joining window."""

from __future__ import annotations

import time
from threading import Event

from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from bluechat.chat.room import Room
from bluechat.ui.console import console


def format_countdown(seconds: int) -> str:
    seconds = max(0, seconds)
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes:02d}:{remainder:02d}"


def show_room_countdown(room: Room, stop_event: Event | None = None) -> bool:
    """Refresh the room code once per second until a guest joins or it expires.

    Returns True if the event stopped the display, otherwise False when the code
    expires. Hosts of group rooms can pass no stop event to keep the joining
    window active for the full five minutes.
    """
    if stop_event is None and not room.group:
        stop_event = room.participant_joined
    joined = False
    with Live(console=console, refresh_per_second=2, transient=False) as live:
        while True:
            remaining = room.remaining_seconds()
            live.update(_room_panel(room, remaining))
            if stop_event is not None and stop_event.is_set():
                joined = True
                break
            if remaining <= 0:
                break
            if stop_event is not None:
                stop_event.wait(min(1.0, float(remaining)))
            else:
                time.sleep(min(1.0, float(remaining)))
    return joined


def _room_panel(room: Room, remaining: int) -> Panel:
    status = "Waiting for participants..." if room.group else "Waiting for a guest..."
    body = Text.assemble(
        ("Connection code\n", "dim"),
        (room.code, "bold cyan"),
        (f"\n\nExpires in {format_countdown(remaining)}\n", "yellow"),
        (status, "dim"),
    )
    return Panel(body, title="BlueChat room", border_style="blue")
