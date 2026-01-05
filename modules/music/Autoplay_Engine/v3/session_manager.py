"""
V3 Autoplay Engine - Session Manager

Manages concurrent autoplay sessions across Discord guilds:
- Limits to 2 concurrent sessions
- Disk persistence for crash recovery
- Session lifecycle (create, update, close)
- Guild-to-session mapping
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .constants import EventType, SessionState, V3Config
from .context_analyzer import ContextAnalyzer, get_context_analyzer
from .event_bus import EventBus, EventPayload
from .novelty_controller import NoveltyController, get_novelty_controller

logger = logging.getLogger(__name__)


@dataclass
class SessionData:
    """Persisted session data."""
    session_id: str
    guild_id: str
    voice_channel_id: str
    created_at: float
    last_activity: float
    state: str  # SessionState value
    play_count: int
    skip_count: int
    current_song_id: Optional[str] = None
    queue_song_ids: list[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> "SessionData":
        """Create from dictionary."""
        return cls(**data)


class SessionManager:
    """
    Manages V3 autoplay sessions with persistence and limits.
    
    Features:
    - Maximum 2 concurrent sessions (configurable)
    - JSON persistence in cache directory
    - Automatic cleanup on crash recovery
    - Session timeout detection
    
    Session Lifecycle:
    1. create_session() - Start new session for a guild
    2. update_activity() - Keep session alive
    3. record_playback() - Update stats on song play
    4. close_session() - Clean shutdown
    
    Persistence:
    - Sessions are saved to ./cache/Autoplay/v3/sessions/
    - Loaded on startup for crash recovery
    - Cleared if guild is no longer in voice
    """
    
    MAX_CONCURRENT_SESSIONS = 2
    SESSION_TIMEOUT_HOURS = 4
    
    def __init__(
        self,
        config: Optional[V3Config] = None,
        context: Optional[ContextAnalyzer] = None,
        novelty: Optional[NoveltyController] = None,
        event_bus: Optional[EventBus] = None
    ):
        """
        Initialize session manager.
        
        Args:
            config: V3 configuration
            context: Context analyzer for session profiles
            novelty: Novelty controller for diversity tracking
            event_bus: Event bus for notifications
        """
        self.config = config or V3Config()
        self.context = context or get_context_analyzer()
        self.novelty = novelty or get_novelty_controller()
        self.event_bus = event_bus or EventBus()
        
        # Active sessions
        self._sessions: dict[str, SessionData] = {}
        self._guild_to_session: dict[str, str] = {}
        
        # Persistence - use cache.base_path from config
        self._sessions_dir = Path(self.config.cache.base_path) / "sessions"
        
        # Lock for concurrent access
        self._lock = asyncio.Lock()
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize and load persisted sessions."""
        if self._initialized:
            return
        
        # Create sessions directory
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        
        # Load persisted sessions
        await self._load_sessions()
        
        # Initialize dependencies
        await self.context.initialize()
        await self.novelty.initialize()
        
        self._initialized = True
        logger.info(f"Session manager initialized with {len(self._sessions)} sessions")
    
    async def shutdown(self) -> None:
        """Persist and clean up all sessions."""
        async with self._lock:
            # Save all sessions
            for session in self._sessions.values():
                await self._persist_session(session)
            
            logger.info(f"Persisted {len(self._sessions)} sessions on shutdown")
        
        self._initialized = False
    
    async def _load_sessions(self) -> None:
        """Load sessions from disk."""
        if not self._sessions_dir.exists():
            return
        
        for session_file in self._sessions_dir.glob("*.json"):
            try:
                with open(session_file, "r") as f:
                    data = json.load(f)
                
                session = SessionData.from_dict(data)
                
                # Check if session is too old
                age_hours = (time.time() - session.last_activity) / 3600
                if age_hours > self.SESSION_TIMEOUT_HOURS:
                    logger.info(f"Discarding stale session {session.session_id}")
                    session_file.unlink()
                    continue
                
                self._sessions[session.session_id] = session
                self._guild_to_session[session.guild_id] = session.session_id
                
                # Restore context analyzer session
                self.context.get_or_create_session(
                    session.session_id,
                    session.guild_id
                )
                
                logger.info(f"Loaded session {session.session_id} for guild {session.guild_id}")
                
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.warning(f"Failed to load session {session_file}: {e}")
                session_file.unlink()
    
    async def _persist_session(self, session: SessionData) -> None:
        """Save session to disk."""
        session_file = self._sessions_dir / f"{session.session_id}.json"
        
        try:
            with open(session_file, "w") as f:
                json.dump(session.to_dict(), f, indent=2)
        except IOError as e:
            logger.error(f"Failed to persist session {session.session_id}: {e}")
    
    async def _remove_session_file(self, session_id: str) -> None:
        """Remove session file from disk."""
        session_file = self._sessions_dir / f"{session_id}.json"
        if session_file.exists():
            session_file.unlink()
    
    async def create_session(
        self,
        guild_id: str,
        voice_channel_id: str
    ) -> Optional[SessionData]:
        """
        Create a new autoplay session.
        
        Args:
            guild_id: Discord guild ID
            voice_channel_id: Voice channel ID
            
        Returns:
            SessionData if created, None if at capacity or already exists
        """
        await self.initialize()
        
        async with self._lock:
            # Check if guild already has a session
            if guild_id in self._guild_to_session:
                existing_id = self._guild_to_session[guild_id]
                return self._sessions.get(existing_id)
            
            # Check capacity
            if len(self._sessions) >= self.MAX_CONCURRENT_SESSIONS:
                logger.warning(
                    f"Cannot create session for guild {guild_id}: "
                    f"at capacity ({self.MAX_CONCURRENT_SESSIONS})"
                )
                return None
            
            # Generate session ID
            session_id = f"{guild_id}_{int(time.time())}"
            now = time.time()
            
            # Create session data
            session = SessionData(
                session_id=session_id,
                guild_id=guild_id,
                voice_channel_id=voice_channel_id,
                created_at=now,
                last_activity=now,
                state=SessionState.COLD.value,
                play_count=0,
                skip_count=0
            )
            
            # Store
            self._sessions[session_id] = session
            self._guild_to_session[guild_id] = session_id
            
            # Initialize context tracking
            self.context.get_or_create_session(session_id, guild_id)
            
            # Persist
            await self._persist_session(session)
            
            # Publish event
            await self.event_bus.publish(EventPayload(
                event_type=EventType.SESSION_STARTED,
                data={
                    "session_id": session_id,
                    "guild_id": guild_id
                }
            ))
            
            logger.info(f"Created session {session_id} for guild {guild_id}")
            
            return session
    
    async def get_session(
        self,
        guild_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Optional[SessionData]:
        """
        Get a session by guild ID or session ID.
        
        Args:
            guild_id: Discord guild ID
            session_id: Session ID
            
        Returns:
            SessionData if found
        """
        await self.initialize()
        
        if session_id:
            return self._sessions.get(session_id)
        
        if guild_id:
            sid = self._guild_to_session.get(guild_id)
            if sid:
                return self._sessions.get(sid)
        
        return None
    
    async def update_activity(
        self,
        session_id: str,
        current_song_id: Optional[str] = None
    ) -> None:
        """
        Update session activity timestamp.
        
        Args:
            session_id: Session identifier
            current_song_id: Currently playing song ID
        """
        session = self._sessions.get(session_id)
        if not session:
            return
        
        session.last_activity = time.time()
        
        if current_song_id:
            session.current_song_id = current_song_id
    
    async def record_playback(
        self,
        session_id: str,
        song_id: str,
        was_skipped: bool,
        duration_played_ms: int,
        total_duration_ms: int
    ) -> None:
        """
        Record a song playback event.
        
        Args:
            session_id: Session identifier
            song_id: Song that was played
            was_skipped: Whether song was skipped
            duration_played_ms: How long it played
            total_duration_ms: Total song duration
        """
        session = self._sessions.get(session_id)
        if not session:
            return
        
        # Update counts
        session.play_count += 1
        if was_skipped:
            session.skip_count += 1
        
        session.last_activity = time.time()
        
        # Update state based on play count
        if session.play_count <= self.config.cold_start_threshold:
            session.state = SessionState.COLD.value
        elif session.play_count <= self.config.warm_threshold:
            session.state = SessionState.WARM.value
        elif session.play_count <= self.config.hot_threshold:
            session.state = SessionState.HOT.value
        else:
            session.state = SessionState.EXTENDED.value
        
        # Forward to context analyzer
        await self.context.record_playback(
            session_id,
            song_id,
            duration_played_ms,
            total_duration_ms,
            not was_skipped
        )
        
        # Persist periodically (every 5 plays)
        if session.play_count % 5 == 0:
            await self._persist_session(session)
    
    async def update_queue(
        self,
        session_id: str,
        queue_song_ids: list[str]
    ) -> None:
        """
        Update session queue state.
        
        Args:
            session_id: Session identifier
            queue_song_ids: Current queue song IDs
        """
        session = self._sessions.get(session_id)
        if not session:
            return
        
        session.queue_song_ids = queue_song_ids
    
    async def close_session(
        self,
        guild_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Optional[SessionData]:
        """
        Close and clean up a session.
        
        Args:
            guild_id: Discord guild ID
            session_id: Session ID
            
        Returns:
            Closed session data
        """
        async with self._lock:
            # Find session
            if session_id:
                session = self._sessions.get(session_id)
            elif guild_id:
                session_id = self._guild_to_session.get(guild_id)
                session = self._sessions.get(session_id) if session_id else None
            else:
                return None
            
            if not session:
                return None
            
            session_id = session.session_id
            guild_id = session.guild_id
            
            # Clean up context
            self.context.end_session(session_id)
            self.novelty.end_session(session_id)
            
            # Remove from tracking
            del self._sessions[session_id]
            if guild_id in self._guild_to_session:
                del self._guild_to_session[guild_id]
            
            # Remove persisted file
            await self._remove_session_file(session_id)
            
            # Publish event
            await self.event_bus.publish(EventPayload(
                event_type=EventType.SESSION_ENDED,
                data={
                    "session_id": session_id,
                    "guild_id": guild_id,
                    "play_count": session.play_count,
                    "skip_count": session.skip_count,
                    "duration_minutes": (time.time() - session.created_at) / 60
                }
            ))
            
            logger.info(
                f"Closed session {session_id}: "
                f"{session.play_count} plays, {session.skip_count} skips"
            )
            
            return session
    
    async def get_active_sessions(self) -> list[SessionData]:
        """Get list of all active sessions."""
        await self.initialize()
        return list(self._sessions.values())
    
    async def get_session_for_guild(self, guild_id: str) -> Optional[SessionData]:
        """Get session for a specific guild."""
        return await self.get_session(guild_id=guild_id)
    
    def get_session_state(self, session_id: str) -> Optional[SessionState]:
        """Get current state for a session."""
        session = self._sessions.get(session_id)
        if not session:
            return None
        return SessionState(session.state)
    
    async def cleanup_stale_sessions(self, active_guild_ids: set[str]) -> int:
        """
        Clean up sessions for guilds no longer in voice.
        
        Args:
            active_guild_ids: Set of guild IDs currently in voice
            
        Returns:
            Number of sessions cleaned up
        """
        async with self._lock:
            stale = []
            
            for session in self._sessions.values():
                if session.guild_id not in active_guild_ids:
                    stale.append(session)
            
            for session in stale:
                await self.close_session(session_id=session.session_id)
            
            if stale:
                logger.info(f"Cleaned up {len(stale)} stale sessions")
            
            return len(stale)
    
    def get_stats(self) -> dict[str, Any]:
        """Get session manager statistics."""
        total_plays = sum(s.play_count for s in self._sessions.values())
        total_skips = sum(s.skip_count for s in self._sessions.values())
        
        return {
            "active_sessions": len(self._sessions),
            "max_sessions": self.MAX_CONCURRENT_SESSIONS,
            "total_plays": total_plays,
            "total_skips": total_skips,
            "avg_skip_rate": total_skips / total_plays if total_plays > 0 else 0,
            "sessions": [
                {
                    "session_id": s.session_id,
                    "guild_id": s.guild_id,
                    "state": s.state,
                    "play_count": s.play_count,
                    "age_minutes": (time.time() - s.created_at) / 60
                }
                for s in self._sessions.values()
            ]
        }


# Singleton instance
_session_manager: Optional[SessionManager] = None


def get_session_manager() -> SessionManager:
    """Get global session manager instance."""
    global _session_manager
    if _session_manager is None:
        _session_manager = SessionManager()
    return _session_manager
