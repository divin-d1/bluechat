"""Cross-platform asynchronous Bluetooth transport contracts."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class BluetoothCapabilities:
    discovery: bool = False
    advertising: bool = False
    hosting: bool = False
    pairing: bool = False
    multiple_peers: bool = False
    max_peers: int = 0


@dataclass(frozen=True, slots=True)
class BluetoothDevice:
    """Backend-neutral discovered peripheral. Address is intentionally optional."""

    name: str
    identifier: str = ""
    address: str | None = None
    paired: bool | None = None
    bluechat_host: bool = False
    details: str | None = None


class PairResult(str, Enum):
    PAIRED = "paired"
    ALREADY_PAIRED = "already_paired"
    USER_ACTION_REQUIRED = "user_action_required"
    UNSUPPORTED = "unsupported"


class Connection(Protocol):
    """Bidirectional logical connection carrying BlueChat protocol frames."""

    peer_id: str

    async def send(self, data: bytes) -> None: ...
    async def receive(self) -> bytes: ...
    async def close(self) -> None: ...


class BluetoothTransport(ABC):
    """OS-neutral Bluetooth transport. Platform objects never cross this boundary."""

    @property
    @abstractmethod
    def capabilities(self) -> BluetoothCapabilities: ...

    @abstractmethod
    async def is_available(self) -> bool: ...

    @abstractmethod
    async def is_enabled(self) -> bool: ...

    @abstractmethod
    async def discover(self, timeout: float = 8.0) -> list[BluetoothDevice]: ...

    @abstractmethod
    async def advertise(self, room_metadata: dict[str, Any]) -> None: ...

    @abstractmethod
    async def stop_advertising(self) -> None: ...

    @abstractmethod
    async def connect(self, device: BluetoothDevice) -> Connection: ...

    @abstractmethod
    async def start_server(self) -> None: ...

    @abstractmethod
    async def accept(self) -> Connection: ...

    @abstractmethod
    async def stop_server(self) -> None: ...

    async def wait_host_failure(self) -> None:
        """Wait until an active native host service or adapter becomes unavailable.

        Backends with lifecycle notifications override this method. The base
        implementation waits forever so client-only/fake transports need not
        synthesize host failures.
        """
        await asyncio.Future()

    @abstractmethod
    async def pair(self, device: BluetoothDevice) -> PairResult: ...

    @abstractmethod
    async def disconnect(self, peer_id: str) -> None: ...
