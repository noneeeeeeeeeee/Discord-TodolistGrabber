"""
V3 Autoplay Engine

A sophisticated music recommendation system for Discord bots that provides
personalized autoplay functionality. Uses a three-layer analysis approach
(Physics, Semantics, Librarian) with session-aware cold/warm/hot state
machine for progressive recommendation quality.

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │                     V3 Autoplay Engine                       │
    ├─────────────────────────────────────────────────────────────┤
    │                                                              │
    │  ┌──────────────┐     ┌──────────────┐     ┌─────────────┐ │
    │  │   Session    │────▶│    Buffer    │────▶│  Playback   │ │
    │  │   Manager    │     │   Manager    │     │   Queue     │ │
    │  └──────────────┘     └──────────────┘     └─────────────┘ │
    │         │                    ▲                              │
    │         ▼                    │                              │
    │  ┌──────────────┐     ┌──────────────┐                     │
    │  │   Context    │────▶│  Recommender │◀─────────┐          │
    │  │   Analyzer   │     │  (Head Chef) │          │          │
    │  └──────────────┘     └──────────────┘          │          │
    │         │                    │                  │          │
    │         ▼                    ▼                  │          │
    │  ┌──────────────┐     ┌──────────────┐   ┌─────────────┐   │
    │  │   Novelty    │     │  Collab.     │   │ Daydreamer  │   │
    │  │  Controller  │     │  Filtering   │   │ (Explorer)  │   │
    │  └──────────────┘     └──────────────┘   └─────────────┘   │
    │                              │                              │
    │  ┌──────────────────────────┴──────────────────────────┐   │
    │  │                    Cache Manager                     │   │
    │  └──────────────────────────────────────────────────────┘   │
    │                              │                              │
    │  ┌──────────────────────────┴──────────────────────────┐   │
    │  │                    Song Analyzer                     │   │
    │  │  ┌─────────┐  ┌───────────┐  ┌──────────────────┐   │   │
    │  │  │ Physics │  │ Semantics │  │     Librarian    │   │   │
    │  │  │(Librosa)│  │(EfficntAT)│  │     (Gemini)     │   │   │
    │  │  └─────────┘  └───────────┘  └──────────────────┘   │   │
    │  └──────────────────────────────────────────────────────┘   │
    │                              │                              │
    │  ┌──────────────────────────┴──────────────────────────┐   │
    │  │              Mappings (Deezer↔YouTube↔Last.fm)       │   │
    │  └──────────────────────────────────────────────────────┘   │
    │                                                              │
    │  ┌──────────────────────────────────────────────────────┐   │
    │  │                      Event Bus                        │   │
    │  └──────────────────────────────────────────────────────┘   │
    └─────────────────────────────────────────────────────────────┘


Usage:
    from modules.music.Autoplay_Engine.v3 import V3Engine

    # Initialize engine
    engine = V3Engine()
    await engine.initialize()

    # Start autoplay session
    session = await engine.start_session(
        guild_id="123456789",
        voice_channel_id="987654321",
        seed_songs=[initial_song]
    )

    # Get next song
    song = await engine.get_next_song(session.session_id)

    # Record playback
    await engine.record_playback(
        session.session_id,
        song.song_id,
        duration_played_ms=180000,
        was_skipped=False
    )

    # End session
    await engine.end_session(session.session_id)
"""

import asyncio
import logging
from typing import Any, Optional

from .buffer_manager import BufferManager, BufferedSong, get_buffer_manager
from .cache_manager import CacheManager, get_cache_manager
from .vector_search_index import VectorSearcher, get_vector_searcher
from .collaborative_recommender import CollaborativeRecommender, get_collaborative_recommender
from .constants import (
    AnalysisMode,
    AnalysisPriority,
    CacheConfig,
    CacheType,
    EventType,
    SessionState,
    SongMetadata,
    V3Config,
    # Layer types
    PhysicsLayer,
    SemanticsLayer,
    LibrarianLayer,
)
from .context_analyzer import ContextAnalyzer, get_context_analyzer
from .daydreamer import Daydreamer, get_daydreamer
from .event_bus import EventBus, EventPayload, get_event_bus
from .gemini_manager import GeminiManager, get_gemini_manager
from .mappings import MappingsManager, SongIdentifier, get_mappings_manager
from .novelty_controller import NoveltyController, get_novelty_controller
from .recommender import Recommendation, Recommender, get_recommender
from .session_manager import SessionData, SessionManager, get_session_manager
from .song_analyzer import SongAnalyzer, get_song_analyzer

logger = logging.getLogger(__name__)

# Import version from centralized constants
from .constants import V3_ENGINE_VERSION, get_v3_version_info

__version__ = V3_ENGINE_VERSION
__all__ = [
    # Main engine
    "V3Engine",
    "AutoplayV3",
    "LastFMAutoplayV3",
    "get_v3_engine",
    "get_lastfm_autoplay_v3",
    
    # Configuration
    "V3Config",
    "CacheConfig",
    
    # Enums
    "SessionState",
    "EventType",
    "AnalysisMode",
    "CacheType",
    "AnalysisPriority",
    
    # Data classes
    "SongMetadata",
    "SongIdentifier",
    "Recommendation",
    "SessionData",
    "BufferedSong",
    "EventPayload",
    # Layer types
    "PhysicsLayer",
    "SemanticsLayer", 
    "LibrarianLayer",
    
    # Managers (for advanced usage)
    "SessionManager",
    "BufferManager",
    "CacheManager",
    "Recommender",
    "SongAnalyzer",
    "ContextAnalyzer",
    "NoveltyController",
    "VectorSearcher",           # Content-based audio similarity
    "CollaborativeRecommender", # User behavior-based recommendations
    "MappingsManager",
    "GeminiManager",
    "Daydreamer",
    "EventBus",
]


class V3Engine:
    """
    Main entry point for V3 Autoplay Engine.
    
    Provides a high-level interface for:
    - Starting/ending autoplay sessions
    - Getting next song recommendations
    - Recording playback events
    - Managing engine lifecycle
    
    All internal complexity (caching, analysis, recommendations)
    is handled automatically.
    """
    
    def __init__(self, config: Optional[V3Config] = None):
        """
        Initialize V3 Engine.
        
        Args:
            config: Optional configuration overrides
        """
        self.config = config or V3Config()
        
        # Core components
        self.event_bus = get_event_bus()
        self.cache = get_cache_manager()
        self.session_mgr = get_session_manager()
        self.buffer_mgr = get_buffer_manager()
        self.recommender = get_recommender()
        self.analyzer = get_song_analyzer()
        self.mappings = get_mappings_manager()
        self.daydreamer = get_daydreamer()
        
        self._initialized = False
        self._gemini_available = False
    
    async def initialize(self) -> None:
        """
        Initialize all engine components.
        
        Must be called before using the engine.
        Components that fail to initialize (e.g., Gemini without keys)
        will be marked unavailable but won't prevent other components from working.
        """
        if self._initialized:
            return
        
        logger.info("Initializing V3 Autoplay Engine...")
        
        await self.event_bus.start()
        logger.debug("Event bus started")
        
        # Core components - must succeed
        await self.cache.initialize()
        await self.mappings.initialize()
        await self.session_mgr.initialize()
        await self.buffer_mgr.initialize()
        
        # Optional components - can fail gracefully
        try:
            await self.analyzer.initialize()
        except Exception as e:
            logger.warning(f"Song analyzer initialization failed (ML models may be unavailable): {e}")
        
        # Gemini is optional - V3 can work with Deezer/Last.fm only
        try:
            from .gemini_manager import get_gemini_manager
            gemini = get_gemini_manager()
            await gemini.initialize()
            self._gemini_available = True
            logger.info("Gemini manager initialized")
        except Exception as e:
            logger.warning(f"Gemini initialization skipped: {e}")
            self._gemini_available = False
        
        await self.recommender.initialize()
        await self.daydreamer.initialize()
        
        # Connect daydreamer to recommender
        self.recommender.set_daydreamer(self.daydreamer)
        
        self._initialized = True
        logger.info("V3 Autoplay Engine initialized successfully")
    
    async def shutdown(self) -> None:
        """
        Gracefully shutdown all engine components.
        
        Persists state and cleans up resources.
        """
        if not self._initialized:
            return
        
        logger.info("Shutting down V3 Autoplay Engine...")
        
        # Shutdown in reverse order
        await self.daydreamer.shutdown()
        await self.recommender.shutdown()
        await self.buffer_mgr.shutdown()
        await self.session_mgr.shutdown()
        await self.analyzer.shutdown()
        await self.cache.shutdown()
        
        # Stop event bus last
        await self.event_bus.stop()
        
        self._initialized = False
        logger.info("V3 Autoplay Engine shutdown complete")
    
    async def start_session(
        self,
        guild_id: str,
        voice_channel_id: str,
        seed_songs: Optional[list[SongIdentifier]] = None
    ) -> Optional[SessionData]:
        """
        Start a new autoplay session.
        
        Args:
            guild_id: Discord guild ID
            voice_channel_id: Voice channel ID
            seed_songs: Initial songs to seed recommendations
            
        Returns:
            SessionData if created, None if at capacity
        """
        await self.initialize()
        
        # Create session
        session = await self.session_mgr.create_session(
            guild_id=guild_id,
            voice_channel_id=voice_channel_id
        )
        
        if not session:
            logger.warning(f"Failed to create session for guild {guild_id}")
            return None
        
        # Fill initial buffer
        if seed_songs:
            await self.buffer_mgr.fill_initial_buffer(
                session_id=session.session_id,
                guild_id=guild_id,
                seed_songs=seed_songs
            )
        
        logger.info(f"Started autoplay session for guild {guild_id}")
        
        return session
    
    async def end_session(
        self,
        guild_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Optional[SessionData]:
        """
        End an autoplay session.
        
        Args:
            guild_id: Discord guild ID
            session_id: Session ID
            
        Returns:
            Final session data
        """
        # Clean up buffer
        if session_id:
            self.buffer_mgr.remove_buffer(session_id)
        
        # Close session
        session = await self.session_mgr.close_session(
            guild_id=guild_id,
            session_id=session_id
        )
        
        if session:
            logger.info(
                f"Ended session: {session.play_count} plays, "
                f"{session.skip_count} skips"
            )
        
        return session
    
    async def get_next_song(
        self,
        session_id: str
    ) -> Optional[BufferedSong]:
        """
        Get the next song to play.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Next buffered song, or None if unavailable
        """
        await self.initialize()
        
        session = await self.session_mgr.get_session(session_id=session_id)
        if not session:
            return None
        
        song = await self.buffer_mgr.get_next_song(
            session_id=session_id,
            guild_id=session.guild_id
        )
        
        if song:
            await self.session_mgr.update_activity(
                session_id,
                current_song_id=song.song_id
            )
        
        return song
    
    async def peek_next_songs(
        self,
        session_id: str
    ) -> list[BufferedSong]:
        """
        Peek at upcoming songs without removing them.
        
        Args:
            session_id: Session identifier
            
        Returns:
            List of upcoming buffered songs
        """
        return await self.buffer_mgr.get_buffer_contents(session_id)
    
    async def record_playback(
        self,
        session_id: str,
        song_id: str,
        duration_played_ms: int,
        total_duration_ms: int,
        was_skipped: bool = False
    ) -> None:
        """
        Record a song playback event.
        
        Args:
            session_id: Session identifier
            song_id: Song that was played
            duration_played_ms: How long it played
            total_duration_ms: Total song duration
            was_skipped: Whether song was skipped
        """
        await self.session_mgr.record_playback(
            session_id=session_id,
            song_id=song_id,
            was_skipped=was_skipped,
            duration_played_ms=duration_played_ms,
            total_duration_ms=total_duration_ms
        )
        
        # Get metadata for novelty tracking
        metadata = await self.cache.get_metadata(song_id)
        if metadata:
            from .novelty_controller import get_novelty_controller
            novelty = get_novelty_controller()
            await novelty.record_played(session_id, metadata)
    
    async def skip_current(
        self,
        session_id: str
    ) -> Optional[BufferedSong]:
        """
        Skip the current song and get the next one.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Next buffered song
        """
        session = await self.session_mgr.get_session(session_id=session_id)
        if not session:
            return None
        
        return await self.buffer_mgr.skip_current(
            session_id=session_id,
            guild_id=session.guild_id
        )
    
    async def resolve_song(
        self,
        title: Optional[str] = None,
        artist: Optional[str] = None,
        deezer_id: Optional[str] = None,
        youtube_id: Optional[str] = None
    ) -> Optional[SongIdentifier]:
        """
        Resolve a song to get full identifier.
        
        Args:
            title: Song title
            artist: Artist name
            deezer_id: Known Deezer ID
            youtube_id: Known YouTube ID
            
        Returns:
            Full SongIdentifier
        """
        await self.initialize()
        
        return await self.mappings.resolve_song(
            title=title,
            artist=artist,
            deezer_id=deezer_id,
            youtube_id=youtube_id
        )
    
    async def get_session_state(
        self,
        session_id: str
    ) -> Optional[SessionState]:
        """
        Get current state of a session.
        
        Args:
            session_id: Session identifier
            
        Returns:
            Current session state
        """
        return self.session_mgr.get_session_state(session_id)
    
    async def get_stats(self) -> dict[str, Any]:
        """
        Get comprehensive engine statistics.
        
        Returns:
            Dictionary with all engine stats
        """
        await self.initialize()
        
        return {
            "version": get_v3_version_info(),
            "sessions": self.session_mgr.get_stats(),
            "cache": await self.cache.get_stats(),
            "analyzer": await self.analyzer.get_queue_status(),
            "buffer": self.buffer_mgr.get_stats(),
            "recommender": await self.recommender.get_stats(),
            "daydreamer": self.daydreamer.get_stats()
        }
    
    async def health_check(self) -> dict[str, bool]:
        """
        Check health of all engine components.
        
        Returns:
            Dictionary of component -> healthy status
        """
        return {
            "initialized": self._initialized,
            "cache": self.cache._initialized if hasattr(self.cache, '_initialized') else False,
            "analyzer": self.analyzer._initialized if hasattr(self.analyzer, '_initialized') else False,
            "session_mgr": self.session_mgr._initialized if hasattr(self.session_mgr, '_initialized') else False,
            "daydreamer": self.daydreamer._running if hasattr(self.daydreamer, '_running') else False
        }


# Singleton instance
_v3_engine: Optional[V3Engine] = None


def get_v3_engine() -> V3Engine:
    """
    Get global V3 Engine instance.
    
    Returns:
        Singleton V3Engine instance
    """
    global _v3_engine
    if _v3_engine is None:
        _v3_engine = V3Engine()
    return _v3_engine


AutoplayV3 = V3Engine


class LastFMAutoplayV3:
    """
    V3 wrapper that provides V1-compatible interface for music_player.py.
    
    Maps V1's interface to V3Engine:
    - is_available() -> True if prerequisites configured (env vars)
    - can_recommend() -> True if prerequisites configured  
    - clear_history(guild_id) -> end_session
    - record_playback_feedback(...) -> record_playback with feedback_type
    - get_recommendations_for_track(track_info, limit) -> get_next_song with resolution
    """
    
    def __init__(self, bot=None):
        """Initialize V3 wrapper with synchronous prerequisite checks."""
        import os
        
        self.bot = bot
        self._engine = get_v3_engine()
        self._guild_sessions: dict[int, str] = {}  # guild_id -> session_id
        self._engine_initialized = False
        self._init_lock = asyncio.Lock()
        
        # Check prerequisites synchronously (like V1 does)
        # This allows is_available() to return True immediately
        self._lastfm_api_key = os.getenv("LASTFM_API_KEY")
        self._prerequisites_available = bool(self._lastfm_api_key)
        
        if self._prerequisites_available:
            logger.info("✅ V3 Autoplay prerequisites configured (Last.fm API key found)")
        else:
            logger.warning(
                "⚠️ LASTFM_API_KEY not found in .env. "
                "Get a free API key from https://www.last.fm/api/account/create"
            )
        
    async def _ensure_initialized(self) -> bool:
        """Ensure engine is initialized (async)."""
        if self._engine_initialized:
            return True
        
        async with self._init_lock:
            if self._engine_initialized:
                return True
            try:
                await self._engine.initialize()
                self._engine_initialized = True
                logger.info("✅ V3 Autoplay Engine fully initialized")
                return True
            except Exception as e:
                logger.error(f"Failed to initialize V3 engine: {e}")
                return False
    
    def start(self) -> None:
        """
        Start the V3 Autoplay Engine (called by music_player on bot start).
        
        This initiates engine initialization including Daydreamer background
        exploration. Since this is called from a synchronous context, we
        create a background task for the async initialization.
        """
        if not self._prerequisites_available:
            logger.warning("Cannot start V3 engine - prerequisites not available")
            return
        
        # Configure logging based on verbosity
        from .constants import configure_v3_logging
        configure_v3_logging()
        
        # Schedule async initialization as background task
        asyncio.create_task(self._start_async())
        logger.info("V3 Autoplay Engine start requested - initializing in background")
    
    async def _start_async(self) -> None:
        """Async startup routine."""
        try:
            await self._ensure_initialized()
            logger.info("✅ V3 Autoplay Engine started with Daydreamer exploration active")
        except Exception as e:
            logger.error(f"Failed to start V3 engine: {e}")
    
    def is_available(self) -> bool:
        """
        Check if autoplay is available (V1 interface).
        
        Returns True if prerequisites (env vars) are configured.
        The async engine initialization happens lazily on first use.
        """
        return self._prerequisites_available
    
    def can_recommend(self) -> bool:
        """Check if recommendations are available (V1 interface)."""
        return self._prerequisites_available
    
    def clear_history(self, guild_id: Optional[int] = None) -> None:
        """Clear recommendation history (V1 interface)."""
        if guild_id is not None:
            session_id = self._guild_sessions.get(guild_id)
            if session_id:
                # Run async in background since V1 signature is sync
                asyncio.create_task(self._engine.end_session(
                    guild_id=str(guild_id),
                    session_id=session_id
                ))
                del self._guild_sessions[guild_id]
                logger.info(f"Cleared V3 session for guild {guild_id}")
        else:
            # Clear all
            for gid, sid in list(self._guild_sessions.items()):
                asyncio.create_task(self._engine.end_session(
                    guild_id=str(gid),
                    session_id=sid
                ))
            self._guild_sessions.clear()
            logger.info("Cleared all V3 sessions")
    
    async def clear_guild_session(self, guild_id: int) -> None:
        """Clear session for a specific guild (async version for music_player)."""
        session_id = self._guild_sessions.get(guild_id)
        if session_id:
            await self._engine.end_session(
                guild_id=str(guild_id),
                session_id=session_id
            )
            del self._guild_sessions[guild_id]
            logger.info(f"Cleared V3 session for guild {guild_id}")
    
    async def record_playback_feedback(
        self,
        guild_id: int,
        artist: str,
        title: str = "",  # V3 uses 'title', V1 uses 'track'
        progress_ratio: float = 1.0,
        *,
        track: str = "",  # V1 parameter name
        feedback_type: Optional[str] = None,  # "more_like_this" or "less_like_this"
        user_id: Optional[int] = None,
        duration_ms: Optional[int] = None,
        primary_listener_bias: bool = True,
    ) -> None:
        """
        Record playback feedback (V1 interface + V3 extensions).
        
        Supports both V1's parameter names and V3's extensions:
        - V1: guild_id, artist, track, progress_ratio, duration_ms
        - V3: adds feedback_type, user_id for button interactions
        """
        if not await self._ensure_initialized():
            return
        
        # Handle V1's 'track' vs V3's 'title'
        track_title = title or track
        if not guild_id or not artist or not track_title:
            return
        
        session_id = self._guild_sessions.get(guild_id)
        if not session_id:
            logger.debug(f"No session for guild {guild_id}, skipping feedback")
            return
        
        # Determine skip status based on progress and feedback type
        was_skipped = progress_ratio < 0.9
        if feedback_type == "less_like_this":
            was_skipped = True  # Treat as skip for recommendation weighting
        elif feedback_type == "more_like_this":
            was_skipped = False  # Treat as full listen
        
        # Calculate duration if not provided
        total_duration_ms = duration_ms or 180000  # Default 3 min
        duration_played_ms = int(total_duration_ms * progress_ratio)
        
        try:
            # Resolve song to get song_id
            song = await self._engine.resolve_song(
                title=track_title,
                artist=artist
            )
            
            if song and song.primary_id:
                await self._engine.record_playback(
                    session_id=session_id,
                    song_id=song.primary_id,
                    duration_played_ms=duration_played_ms,
                    total_duration_ms=total_duration_ms,
                    was_skipped=was_skipped
                )
                
                if feedback_type:
                    logger.debug(
                        f"Recorded {feedback_type} feedback: {artist} - {track_title} "
                        f"(user={user_id})"
                    )
            else:
                logger.debug(f"Could not resolve song for feedback: {artist} - {track_title}")
                
        except Exception as e:
            logger.error(f"Failed to record V3 feedback: {e}")
    
    async def get_recommendations_for_track(
        self,
        track_info: dict[str, Any],
        limit: int = 10
    ) -> list[tuple[str, dict[str, Any]]]:
        """
        Get recommendations based on current track (V1 interface).
        
        Args:
            track_info: Dict with 'title', 'author', 'length', 'guild_id'
            limit: Number of recommendations
            
        Returns:
            List of (url, track_dict) tuples - V1 format for music_player
        """
        if not await self._ensure_initialized():
            return []
        
        raw_title = track_info.get("title", "").strip()
        artist = track_info.get("author", "").strip()
        guild_id = int(track_info.get("guild_id", 0) or 0)
        
        if not raw_title:
            logger.warning("Missing track title for V3 autoplay")
            return []
        
        try:
            # Ensure session exists
            session_id = self._guild_sessions.get(guild_id)
            if not session_id:
                # Create new session
                session = await self._engine.start_session(
                    guild_id=str(guild_id),
                    voice_channel_id="0"  # Not used in V3
                )
                if session:
                    session_id = session.session_id
                    self._guild_sessions[guild_id] = session_id
                else:
                    logger.error(f"Failed to create session for guild {guild_id}")
                    return []
            
            # Resolve seed song
            seed = await self._engine.resolve_song(
                title=raw_title,
                artist=artist
            )
            
            if not seed:
                logger.warning(f"Could not resolve seed: {artist} - {raw_title}")
                return []
            
            # Fill buffer with seed if needed
            await self._engine.buffer_mgr.fill_initial_buffer(
                session_id=session_id,
                guild_id=str(guild_id),
                seed_songs=[seed]
            )
            
            # Get recommendations
            recommendations = []
            for _ in range(limit):
                song = await self._engine.get_next_song(session_id)
                if not song:
                    break
                
                # Build V1-compatible result
                # The URL should be the youtube URL for playback
                url = None
                if song.identifier.youtube_id:
                    url = f"https://www.youtube.com/watch?v={song.identifier.youtube_id}"
                elif song.identifier.deezer_id:
                    # Will need resolution by music_player
                    url = f"deezer:{song.identifier.deezer_id}"
                
                if url:
                    track_dict = {
                        "artist": song.identifier.artist or artist,
                        "title": song.identifier.title or "Unknown",
                        "url": url,
                        "deezer_id": song.identifier.deezer_id,
                        "youtube_id": song.identifier.youtube_id,
                        "source": getattr(song, 'source', 'v3'),
                        "confidence": getattr(song, 'confidence', 0.8)
                    }
                    recommendations.append((url, track_dict))
                    
                    logger.info(
                        f"[V3] Recommendation: {track_dict['artist']} - {track_dict['title']}"
                    )
            
            return recommendations
            
        except Exception as e:
            logger.error(f"V3 get_recommendations failed: {e}")
            import traceback
            traceback.print_exc()
            return []
    
    async def get_stats(self) -> dict[str, Any]:
        """Get V3 engine statistics."""
        return await self._engine.get_stats()
    
    async def health_check(self) -> dict[str, bool]:
        """Check health of V3 engine."""
        return await self._engine.health_check()


# Singleton wrapper instance
_lastfm_autoplay_v3: Optional[LastFMAutoplayV3] = None


def get_lastfm_autoplay_v3(bot=None) -> LastFMAutoplayV3:
    """
    Get or create global V3 autoplay wrapper instance.
    
    This is the entry point used by config.py to get the V3 engine
    with a V1-compatible interface.
    
    Args:
        bot: Discord bot instance (optional, for compatibility)
        
    Returns:
        LastFMAutoplayV3 wrapper instance
    """
    global _lastfm_autoplay_v3
    if _lastfm_autoplay_v3 is None:
        _lastfm_autoplay_v3 = LastFMAutoplayV3(bot)
    return _lastfm_autoplay_v3
