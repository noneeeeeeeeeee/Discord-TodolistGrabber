import asyncio
import hashlib
import json
import logging
import os
import random
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
    Collects feedback signals and emits session-scoped telemetry per guild.

    Telemetry files are stored as `telemetry/{guild_id}.jsonl` and are:
    - Session-based: Created when first event is recorded for a guild
    - Cleared when bot leaves VC (session ends)
    - Used for feedback-based scoring and adaptation (Issue #3)

    Later, a global collaborative filtering telemetry file will be added
    for cross-guild recommendations.
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

        # Per-guild telemetry files: telemetry/{guild_id}.jsonl
        self._guild_telemetry_files: Dict[str, Path] = {}

        self._retention_window = max(1, retention_days) * _SECONDS_PER_DAY
        self._global_buffer: Deque[TelemetryEvent] = deque(
            maxlen=max(1, global_buffer_size)
        )

        # Track active sessions per guild
        self._active_sessions: Dict[str, str] = {}  # guild_id -> session_id

        self._salt = self._resolve_salt(salt)
        self._io_lock = asyncio.Lock()

    async def record_event(
        self,
        *,
        guild_id: Optional[int | str],
        user_id: Optional[int | str],  # Kept for API compatibility
        track_id: str,
        event_type: str,
        timestamp: Optional[float] = None,
        session_id: Optional[str] = None,
        source: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Record a feedback event for a specific guild.

        Events are written to `telemetry/{guild_id}.jsonl` for session-based tracking.

        Returns:
            True if the event was recorded successfully.
        """
        if not guild_id:
            LOG.debug("No guild_id provided; skipping telemetry")
            return False

        if not track_id or not track_id.strip():
            LOG.debug("Empty track_id; skipping telemetry")
            return False

        event_type = event_type.strip().lower()
        if event_type not in _ALLOWED_EVENT_TYPES:
            LOG.warning("Unknown event type '%s'; coercing to 'custom'", event_type)
            event_type = "custom"

        ts = timestamp if timestamp is not None else time.time()

        # Get or create session ID for this guild
        guild_str = str(guild_id)
        if guild_str not in self._active_sessions:
            self._active_sessions[guild_str] = self._generate_session_id()

        sess_id = session_id or self._active_sessions.get(guild_str)

        event = TelemetryEvent(
            track_id=track_id.strip(),
            event_type=event_type,
            timestamp=ts,
            session_id=sess_id,
            source=source,
            metadata=metadata,
        )

        self._global_buffer.append(event)

        # Write to guild-specific file
        try:
            await self._write_guild_event(guild_str, event)
            return True
        except Exception as exc:
            LOG.error(
                "Failed to write telemetry event for guild %s: %s", guild_str, exc
            )
            return False

    async def clear_guild_session(self, guild_id: int | str) -> bool:
        """
        Clear the session for a guild and delete its telemetry file.
        Called when bot leaves VC to reset session state.

        Returns:
            True if session was cleared successfully.
        """
        guild_str = str(guild_id)

        # Remove active session
        if guild_str in self._active_sessions:
            del self._active_sessions[guild_str]

        # Delete guild telemetry file
        guild_file = self._telemetry_dir / f"{guild_str}.jsonl"
        try:
            if guild_file.exists():
                guild_file.unlink()
                LOG.info("🗑️ [Telemetry] Cleared session file for guild %s", guild_str)
            if guild_str in self._guild_telemetry_files:
                del self._guild_telemetry_files[guild_str]
            return True
        except Exception as exc:
            LOG.error("Failed to clear session file for guild %s: %s", guild_str, exc)
            return False

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
        """Get recent telemetry events for a specific guild."""
        cutoff = time.time() - self._retention_window
        allowed_types = (
            {item.lower().strip() for item in event_types} if event_types else None
        )

        buffer = self._global_buffer
        self._purge_buffer(buffer, cutoff)

        results: List[Dict[str, Any]] = []

        # Filter by guild session if provided
        guild_session = self._active_sessions.get(str(guild_id)) if guild_id else None

        for event in reversed(buffer):
            if allowed_types and event.event_type not in allowed_types:
                continue
            if guild_session and event.session_id != guild_session:
                continue
            results.append(event.to_dict())
            if len(results) >= max(1, limit):
                break
        return results

    def set_guild_opt_out(self, guild_id: int | str, enabled: bool) -> None:
        """Deprecated: Session-based telemetry doesn't need opt-out."""
        LOG.warning(
            "set_guild_opt_out is deprecated - telemetry is now session-based per guild"
        )
        pass

    def set_user_opt_out(self, user_id: int | str, enabled: bool) -> None:
        """Deprecated: Session-based telemetry doesn't track users."""
        LOG.warning(
            "set_user_opt_out is deprecated - telemetry is now session-based per guild"
        )
        pass

    def get_buffer_stats(self) -> Dict[str, Any]:
        cutoff = time.time() - self._retention_window
        self._purge_buffer(self._global_buffer, cutoff)
        return {
            "global_events": len(self._global_buffer),
            "active_sessions": len(self._active_sessions),
            "guild_files": len(self._guild_telemetry_files),
        }

    # -------------------------------------------------------------------------
    # Private Helpers
    # -------------------------------------------------------------------------
    def _generate_session_id(self) -> str:
        """Generate unique session ID for guild."""
        timestamp = int(time.time() * 1000)
        random_suffix = "".join(random.choices("0123456789abcdef", k=6))
        return f"{timestamp}_{random_suffix}"

    async def _write_guild_event(self, guild_id: str, event: TelemetryEvent) -> None:
        """Write event to guild-specific telemetry file."""
        guild_file = self._telemetry_dir / f"{guild_id}.jsonl"

        # Track which files we're writing to
        if guild_id not in self._guild_telemetry_files:
            self._guild_telemetry_files[guild_id] = guild_file

        async with self._io_lock:
            try:
                with open(guild_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event.to_dict()) + "\n")
            except Exception as exc:
                LOG.error(
                    "Failed to write event to guild %s telemetry: %s", guild_id, exc
                )

    async def flush(self) -> None:
        """No-op for API compatibility; individual writes flush immediately."""
        pass

    def _cache_event(self, event: TelemetryEvent, now: float) -> None:
        """Cache event in global buffer for recent event queries."""
        cutoff = now - self._retention_window
        self._purge_buffer(self._global_buffer, cutoff)
        self._global_buffer.append(event)

    def _purge_buffer(self, buffer: Deque[TelemetryEvent], cutoff: float) -> None:
        while buffer and buffer[0].timestamp < cutoff:
            buffer.popleft()

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
