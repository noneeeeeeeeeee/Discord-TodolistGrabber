import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence

LOG = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86400
_ALLOWED_EVENT_TYPES = {
    "play",
    "finish",
    "skip",
    "hard_skip",
    "like",
    "dislike",
    "replay",
    "stale",
    "custom",
}


@dataclass
class TelemetryEvent:
    guild_id: str
    user_id: str
    track_id: str
    event_type: str
    timestamp: float
    session_id: Optional[str] = None
    source: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "guild": self.guild_id,
            "user": self.user_id,
            "track": self.track_id,
            "event": self.event_type,
            "timestamp": self.timestamp,
        }
        if self.session_id:
            payload["session"] = self.session_id
        if self.source:
            payload["source"] = self.source
        if self.metadata:
            payload["meta"] = self.metadata
        return payload


class FeedbackManager:
    """Collects per-guild feedback signals and emits anonymized telemetry."""

    def __init__(
        self,
        cache_dir: Path | str = Path("cache/music"),
        *,
        retention_days: int = 365,
        guild_buffer_size: int = 512,
        global_buffer_size: int = 4096,
        salt: Optional[str] = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._telemetry_dir = self._cache_dir / "telemetry"
        self._telemetry_dir.mkdir(parents=True, exist_ok=True)
        self._telemetry_file = self._telemetry_dir / "events.jsonl"

        self._retention_window = max(1, retention_days) * _SECONDS_PER_DAY
        self._guild_buffer_size = max(1, guild_buffer_size)
        self._global_buffer: Deque[TelemetryEvent] = deque(maxlen=max(1, global_buffer_size))
        self._guild_buffers: Dict[str, Deque[TelemetryEvent]] = {}

        self._guild_opt_out: set[str] = set()
        self._user_opt_out: set[str] = set()

        self._salt = self._resolve_salt(salt)
        self._io_lock = asyncio.Lock()

    async def record_event(
        self,
        *,
        guild_id: Optional[int | str],
        user_id: Optional[int | str],
        track_id: str,
        event_type: str,
        timestamp: Optional[float] = None,
        session_id: Optional[str] = None,
        source: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if not track_id:
            return False

        hashed_guild = self._hash_identifier(guild_id)
        hashed_user = self._hash_identifier(user_id)

        if hashed_guild in self._guild_opt_out or hashed_user in self._user_opt_out:
            return False

        safe_event = event_type.lower().strip() if event_type else "unknown"
        if safe_event not in _ALLOWED_EVENT_TYPES:
            LOG.debug("Unknown telemetry event_type=%s; coercing to 'custom'", event_type)
            safe_event = "custom"

        metadata_payload = metadata or {}
        now = timestamp or time.time()
        event = TelemetryEvent(
            guild_id=hashed_guild,
            user_id=hashed_user,
            track_id=track_id,
            event_type=safe_event,
            timestamp=now,
            session_id=session_id,
            source=source,
            metadata=metadata_payload or None,
        )

        self._cache_event(event, hashed_guild, now)
        await self._append_event(event)
        return True

    async def record_events(
        self,
        events: Sequence[Dict[str, Any]],
    ) -> int:
        recorded = 0
        for payload in events:
            success = await self.record_event(**payload)
            if success:
                recorded += 1
        return recorded

    def get_recent_events(
        self,
        *,
        guild_id: Optional[int | str] = None,
        limit: int = 50,
        event_types: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        cutoff = time.time() - self._retention_window
        allowed_types = {item.lower().strip() for item in event_types} if event_types else None

        if guild_id is None:
            buffer = self._global_buffer
        else:
            hashed = self._hash_identifier(guild_id)
            buffer = self._guild_buffers.get(hashed, deque())

        self._purge_buffer(buffer, cutoff)

        results: List[Dict[str, Any]] = []
        for event in reversed(buffer):
            if allowed_types and event.event_type not in allowed_types:
                continue
            results.append(event.to_dict())
            if len(results) >= max(1, limit):
                break
        return results

    def set_guild_opt_out(self, guild_id: int | str, enabled: bool) -> None:
        hashed = self._hash_identifier(guild_id)
        if enabled:
            self._guild_opt_out.add(hashed)
        else:
            self._guild_opt_out.discard(hashed)

    def set_user_opt_out(self, user_id: int | str, enabled: bool) -> None:
        hashed = self._hash_identifier(user_id)
        if enabled:
            self._user_opt_out.add(hashed)
        else:
            self._user_opt_out.discard(hashed)

    def get_buffer_stats(self) -> Dict[str, Any]:
        cutoff = time.time() - self._retention_window
        self._purge_buffer(self._global_buffer, cutoff)
        for buffer in self._guild_buffers.values():
            self._purge_buffer(buffer, cutoff)
        return {
            "global_events": len(self._global_buffer),
            "guilds_tracked": len(self._guild_buffers),
            "guild_opt_out": len(self._guild_opt_out),
            "user_opt_out": len(self._user_opt_out),
        }

    async def flush(self) -> None:
        if not self._telemetry_file.exists():
            return
        # noop placeholder for API symmetry; individual writes flush immediately

    def _cache_event(self, event: TelemetryEvent, hashed_guild: str, now: float) -> None:
        cutoff = now - self._retention_window
        self._purge_buffer(self._global_buffer, cutoff)
        self._global_buffer.append(event)

        buffer = self._guild_buffers.get(hashed_guild)
        if buffer is None:
            buffer = deque(maxlen=self._guild_buffer_size)
            self._guild_buffers[hashed_guild] = buffer
        else:
            self._purge_buffer(buffer, cutoff)
        buffer.append(event)

    def _purge_buffer(self, buffer: Deque[TelemetryEvent], cutoff: float) -> None:
        while buffer and buffer[0].timestamp < cutoff:
            buffer.popleft()

    async def _append_event(self, event: TelemetryEvent) -> None:
        line = json.dumps(event.to_dict(), separators=(",", ":")) + "\n"
        async with self._io_lock:
            await asyncio.to_thread(self._write_line, line)

    def _write_line(self, line: str) -> None:
        with self._telemetry_file.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _resolve_salt(self, provided: Optional[str]) -> bytes:
        if provided:
            return provided.encode("utf-8")

        env_value = os.getenv("AUTOPLAY_HASH_SALT")
        if env_value:
            return env_value.encode("utf-8")

        salt_file = self._telemetry_dir / "salt.txt"
        if salt_file.exists():
            try:
                data = salt_file.read_text(encoding="utf-8").strip()
                if data:
                    return data.encode("utf-8")
            except OSError:
                pass

        generated = secrets.token_hex(16)
        try:
            salt_file.write_text(generated, encoding="utf-8")
        except OSError:
            LOG.debug("Failed to persist telemetry salt; falling back to memory only")
        return generated.encode("utf-8")

    def _hash_identifier(self, value: Optional[int | str]) -> str:
        if value is None:
            return "anon"
        raw = str(value).encode("utf-8")
        digest = hashlib.sha256(self._salt + raw).hexdigest()
        return digest


__all__ = ["FeedbackManager", "TelemetryEvent"]
