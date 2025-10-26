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
    """
    Telemetry event for genre-based tracking (NO guild/user tracking).
    Tracks purely music-related data for collaborative filtering.
    """
    track_id: str
    event_type: str
    timestamp: float
    session_id: Optional[str] = None
    source: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
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
    """
    Collects feedback signals and emits anonymized telemetry.
    
    Updated: Telemetry is now PURELY GENRE-BASED (no guild/user tracking).
    This allows for better collaborative filtering without privacy concerns.
    """

    def __init__(
        self,
        cache_dir: Path | str = Path("cache/music"),
        *,
        retention_days: int = 365,
        global_buffer_size: int = 4096,
        salt: Optional[str] = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

        self._telemetry_dir = self._cache_dir / "telemetry"
        self._telemetry_dir.mkdir(parents=True, exist_ok=True)
        self._telemetry_file = self._telemetry_dir / "events.jsonl"

        self._retention_window = max(1, retention_days) * _SECONDS_PER_DAY
        self._global_buffer: Deque[TelemetryEvent] = deque(maxlen=max(1, global_buffer_size))

        # Removed: guild/user opt-out and guild-specific buffers
        # Telemetry is now fully anonymous and genre-based

        self._salt = self._resolve_salt(salt)
        self._io_lock = asyncio.Lock()

    async def record_event(
        self,
        *,
        guild_id: Optional[int | str],  # Ignored but kept for API compatibility
        user_id: Optional[int | str],   # Ignored but kept for API compatibility
        track_id: str,
        event_type: str,
        timestamp: Optional[float] = None,
        session_id: Optional[str] = None,
        source: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Record a telemetry event (genre-based, no guild/user tracking).
        
        guild_id and user_id are ignored but kept for backward compatibility.
        """
        if not track_id:
            return False

        safe_event = event_type.lower().strip() if event_type else "unknown"
        if safe_event not in _ALLOWED_EVENT_TYPES:
            LOG.debug("Unknown telemetry event_type=%s; coercing to 'custom'", event_type)
            safe_event = "custom"

        metadata_payload = metadata or {}
        now = timestamp or time.time()
        event = TelemetryEvent(
            track_id=track_id,
            event_type=safe_event,
            timestamp=now,
            session_id=session_id,
            source=source,
            metadata=metadata_payload or None,
        )

        self._cache_event(event, now)
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
        guild_id: Optional[int | str] = None,  # Ignored but kept for API compatibility
        limit: int = 50,
        event_types: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Get recent telemetry events (genre-based, no guild filtering)."""
        cutoff = time.time() - self._retention_window
        allowed_types = {item.lower().strip() for item in event_types} if event_types else None

        buffer = self._global_buffer
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
        """Deprecated: No longer tracks guild-specific data."""
        LOG.warning("set_guild_opt_out is deprecated - telemetry is now genre-based only")
        pass

    def set_user_opt_out(self, user_id: int | str, enabled: bool) -> None:
        """Deprecated: No longer tracks user-specific data."""
        LOG.warning("set_user_opt_out is deprecated - telemetry is now genre-based only")
        pass

    def get_buffer_stats(self) -> Dict[str, Any]:
        cutoff = time.time() - self._retention_window
        self._purge_buffer(self._global_buffer, cutoff)
        return {
            "global_events": len(self._global_buffer),
            "genre_based": True,
            "privacy_mode": "anonymous",
        }

    async def flush(self) -> None:
        if not self._telemetry_file.exists():
            return
        # noop placeholder for API symmetry; individual writes flush immediately

    def _cache_event(self, event: TelemetryEvent, now: float) -> None:
        """Cache event in global buffer only (no per-guild tracking)."""
        cutoff = now - self._retention_window
        self._purge_buffer(self._global_buffer, cutoff)
        self._global_buffer.append(event)

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
        """Deprecated: Salt no longer used for genre-based telemetry."""
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
        """Deprecated: No longer hashes guild/user IDs."""
        if value is None:
            return "anon"
        raw = str(value).encode("utf-8")
        digest = hashlib.sha256(self._salt + raw).hexdigest()
        return digest


__all__ = ["FeedbackManager", "TelemetryEvent"]
