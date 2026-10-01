"""Optional, local-only JSONL conversation history."""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_data_path

from bluechat.config.models import HistoryPreference

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
logger = logging.getLogger(__name__)


class HistoryManager:
    """Append structured local events without persisting authentication data."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path(user_data_path("BlueChat", "BlueChat")) / "History").expanduser()
        self._lock = threading.Lock()

    @staticmethod
    def should_save(preference: HistoryPreference, *, consent: bool | None = None) -> bool:
        if preference is HistoryPreference.ALWAYS:
            return True
        if preference is HistoryPreference.NEVER:
            return False
        return consent is True

    def create(
        self,
        peer_name: str,
        *,
        room_id: str,
        preference: HistoryPreference,
        consent: bool | None = None,
    ) -> Path | None:
        if not self.should_save(preference, consent=consent):
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        safe_peer = _SAFE.sub("_", peer_name.strip())[:48].strip("_") or "peer"
        safe_room = _SAFE.sub("", room_id[:16]) or "room"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        suffix = 0
        while True:
            discriminator = f"_{suffix}" if suffix else ""
            path = self.root / f"{stamp}_{safe_peer}_{safe_room[:8]}{discriminator}.jsonl"
            try:
                descriptor = path.open("x", encoding="utf-8")
            except FileExistsError:
                suffix += 1
                continue
            with descriptor:
                try:
                    path.chmod(0o600)
                except OSError as exc:
                    logger.debug(
                        "Could not restrict history file permissions (%s)", type(exc).__name__
                    )
            return path

    def append(
        self,
        path: Path | None,
        kind: str,
        *,
        sender: str | None = None,
        text: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if path is None:
            return
        if kind not in {"message", "joined", "left", "file"}:
            raise ValueError("unsupported history event")
        record: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": kind,
        }
        if sender is not None:
            record["sender"] = sender[:32]
        if text is not None:
            record["text"] = text[:16_000]
        if metadata:
            # Only caller-selected benign transfer metadata should be passed.
            record["metadata"] = metadata
        raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._lock, path.open("a", encoding="utf-8") as stream:
                stream.write(raw)
                stream.flush()
        except OSError as exc:
            logger.warning("Local chat history could not be written (%s)", type(exc).__name__)
