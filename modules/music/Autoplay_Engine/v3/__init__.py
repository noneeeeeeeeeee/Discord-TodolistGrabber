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

Session States:
    - COLD (1-10 songs): Last.fm similarity, safe picks
    - WARM (11-25 songs): Hybrid recommendations, light CF
    - HOT (25+ songs): Full CF + context + novelty
    - EXTENDED (25+ special): Daydreamer integration

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
    CacheConfig,
    CacheType,
    EventType,
    SessionState,
    SongMetadata,
    V3Config,
)
from .context_analyzer import ContextAnalyzer, get_context_analyzer
from .daydreamer import Daydreamer, get_daydreamer
from .event_bus import EventBus, EventPayload, get_event_bus
from .gemini_manager import GeminiManager, get_gemini_manager
from .mappings import MappingsManager, SongIdentifier, get_mappings_manager
from .novelty_controller import NoveltyController, get_novelty_controller
from .recommender import Recommendation, Recommender, get_recommender
from .session_manager import SessionData, SessionManager, get_session_manager
from .song_analyzer import AnalysisPriority, SongAnalyzer, get_song_analyzer

logger = logging.getLogger(__name__)

__version__ = "3.0.0"
__all__ = [
    # Main engine
    "V3Engine",
    "get_v3_engine",
    
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
    
    async def initialize(self) -> None:
        """
        Initialize all engine components.
        
        Must be called before using the engine.

        """
        if self._initialized:
            return
        
        logger.info("Initializing V3 Autoplay Engine...")
        

        await self.event_bus.start()
        logger.debug("Event bus started")
        

        await self.cache.initialize()
        await self.mappings.initialize()
        await self.analyzer.initialize()
        await self.session_mgr.initialize()
        await self.buffer_mgr.initialize()
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
            "version": __version__,
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
