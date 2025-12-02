import asyncio
import json
import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Sequence

LOG = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86400
_SKIP_DECAY_DAYS = 7  # Skip penalties decay over 7 days
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


@dataclass
class SkipPenalty:
    """
    Track-level skip penalty with timestamp for decay calculation.
    
    Apple Music-style decay: penalties reduce over 7 days.
    
    NOTE: Currently stored in memory only - lost on session end/bot restart.
    Future implementation will persist to guild telemetry files and restore
    on session start. This ties into the export/import autoplay feature (V2).
    
    For now, skip penalties only persist within a single session.
    """
    track_id: str
    penalty: float  # Base penalty (-0.3 for skip, -0.5 for "less like this")
    timestamp: float
    
    def get_decayed_penalty(self, now: Optional[float] = None) -> float:
        """
        Calculate penalty with 7-day exponential decay.
        
        Formula: decayed_penalty = penalty * exp(-days_elapsed / 7)
        
        Returns:
            Decayed penalty (approaches 0 over time)
        """
        if now is None:
            now = time.time()
        
        days_elapsed = (now - self.timestamp) / _SECONDS_PER_DAY
        decay_factor = math.exp(-days_elapsed / _SKIP_DECAY_DAYS)
        
        return self.penalty * decay_factor


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

        # Skip Penalty Decay System (Apple Music style)
        # Tracks: {track_id: SkipPenalty} - penalties decay over 7 days
        # NOTE: In-memory only for now. Persistence requires export/import feature (V2)
        # Future: Load from guild telemetry on session start, save on session end
        self._skip_penalties: Dict[str, SkipPenalty] = {}
        
        # Artist session skip counter (resets each session)
        # {guild_id: {artist: skip_count}}
        self._artist_session_skips: Dict[str, Dict[str, int]] = {}

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
        
        # Reset artist session skip counter
        if guild_str in self._artist_session_skips:
            del self._artist_session_skips[guild_str]

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

    def get_buffer_stats(self) -> Dict[str, Any]:
        cutoff = time.time() - self._retention_window
        self._purge_buffer(self._global_buffer, cutoff)
        return {
            "global_events": len(self._global_buffer),
            "active_sessions": len(self._active_sessions),
            "guild_files": len(self._guild_telemetry_files),
            "tracked_skip_penalties": len(self._skip_penalties),
        }

    # -------------------------------------------------------------------------
    # Skip Penalty Decay System (Apple Music style)
    # -------------------------------------------------------------------------
    def record_skip_penalty(
        self,
        track_id: str,
        *,
        penalty: float = -0.3,
        guild_id: Optional[str] = None,
        artist: Optional[str] = None,
    ) -> None:
        """
        Record a skip penalty for a track with decay timestamp.
        
        Penalty weights:
        - Quick skip (<15s): -0.3
        - "Less Like This": -0.5
        - Artist skip x2 in session: -0.1 additional
        
        Args:
            track_id: Track identifier
            penalty: Penalty value (negative)
            guild_id: Guild for artist session tracking
            artist: Artist name for session skip counting
        """
        now = time.time()
        
        # Stack penalties if track already has one (additive)
        if track_id in self._skip_penalties:
            existing = self._skip_penalties[track_id]
            # Combine penalties (more negative = stronger dislike)
            combined_penalty = max(-1.0, existing.penalty + penalty)
            self._skip_penalties[track_id] = SkipPenalty(
                track_id=track_id,
                penalty=combined_penalty,
                timestamp=now,  # Reset decay clock
            )
        else:
            self._skip_penalties[track_id] = SkipPenalty(
                track_id=track_id,
                penalty=penalty,
                timestamp=now,
            )
        
        # Track artist session skips
        if guild_id and artist:
            guild_str = str(guild_id)
            if guild_str not in self._artist_session_skips:
                self._artist_session_skips[guild_str] = {}
            
            artist_lower = artist.lower()
            self._artist_session_skips[guild_str][artist_lower] = (
                self._artist_session_skips[guild_str].get(artist_lower, 0) + 1
            )
            
            # 2+ artist skips in session = additional penalty
            if self._artist_session_skips[guild_str][artist_lower] >= 2:
                LOG.debug(
                    "📉 [Feedback] Artist '%s' skipped %d times this session, applying -0.1 penalty",
                    artist,
                    self._artist_session_skips[guild_str][artist_lower],
                )

    def record_less_like_this(
        self,
        track_id: str,
        embedding: Optional[List[float]] = None,
    ) -> None:
        """
        Record "Less Like This" explicit dislike.
        
        Per spec:
        - Track penalty: -0.5
        - Similar embeddings penalty: -0.2 (if provided)
        
        Args:
            track_id: Track identifier
            embedding: Track embedding for similarity penalty (future use)
        """
        self.record_skip_penalty(track_id, penalty=-0.5)
        
        # TODO: When collaborative matrix is ready, apply -0.2 to similar embeddings
        if embedding:
            LOG.debug(
                "📉 [Feedback] 'Less Like This' recorded for %s (embedding penalty pending)",
                track_id,
            )

    def get_track_penalty(self, track_id: str) -> float:
        """
        Get current decayed penalty for a track.
        
        Returns:
            Decayed penalty value (0.0 if no penalty or fully decayed)
        """
        if track_id not in self._skip_penalties:
            return 0.0
        
        penalty = self._skip_penalties[track_id]
        decayed = penalty.get_decayed_penalty()
        
        # Remove if penalty has decayed to negligible
        if abs(decayed) < 0.01:
            del self._skip_penalties[track_id]
            return 0.0
        
        return decayed

    def get_feedback_multiplier(self, track_id: str) -> float:
        """
        Get feedback multiplier for scoring (1.0 + penalty).
        
        Returns:
            Multiplier for candidate scoring (0.5 to 1.0 range)
        """
        penalty = self.get_track_penalty(track_id)
        # Convert penalty to multiplier (penalty is negative, so this reduces score)
        multiplier = 1.0 + penalty
        return max(0.5, min(1.0, multiplier))

    def get_artist_session_skips(self, guild_id: str, artist: str) -> int:
        """Get number of times an artist was skipped this session."""
        guild_str = str(guild_id)
        if guild_str not in self._artist_session_skips:
            return 0
        return self._artist_session_skips[guild_str].get(artist.lower(), 0)

    def get_artist_penalty(self, guild_id: str, artist: str) -> float:
        """
        Get session-based artist penalty.
        
        Per spec: 2+ artist skips/session = -0.1 affinity (resets next session)
        
        Returns:
            Penalty value (0.0 or -0.1)
        """
        skip_count = self.get_artist_session_skips(guild_id, artist)
        if skip_count >= 2:
            return -0.1
        return 0.0

    def cleanup_decayed_penalties(self) -> int:
        """
        Remove fully decayed penalties to free memory.
        
        Returns:
            Number of penalties removed
        """
        now = time.time()
        to_remove = []
        
        for track_id, penalty in self._skip_penalties.items():
            if abs(penalty.get_decayed_penalty(now)) < 0.01:
                to_remove.append(track_id)
        
        for track_id in to_remove:
            del self._skip_penalties[track_id]
        
        if to_remove:
            LOG.debug(
                "🧹 [Feedback] Cleaned up %d fully decayed penalties",
                len(to_remove),
            )
        
        return len(to_remove)

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



__all__ = ["FeedbackManager", "TelemetryEvent", "SkipPenalty"]
