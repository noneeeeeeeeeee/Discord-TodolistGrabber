"""
Track Fetcher (Daydreamer) - V3 Autoplay Engine.

This module is responsible ONLY for discovering and fetching tracks.
It delegates all enrichment to the EnrichmentWorker.

Scenarios:
A. First Run (Empty Cache): Fetch 200 tracks from Last.fm chart.getTopTracks
B. Daydreaming: Explore tracks using Last.fm track.getSimilar / tag.getTopTracks
C. New Releases: Monthly check using Deezer editorial releases

The Track Fetcher does NOT:
- Resolve Deezer metadata (EnrichmentWorker does this)
- Run audio analysis (EnrichmentWorker does this)
- Call Gemini (EnrichmentWorker does this)
"""

import asyncio
import json
import logging
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Set, Optional, List, Dict, Any, Tuple

from .config import (
    DEFAULT_VERBOSITY,
    FIRST_RUN_FETCH_COUNT,
    DAYDREAM_BATCH_SIZE,
    DAYDREAM_INTERVAL_SECONDS,
    NEW_RELEASE_CHECK_DAYS,
    PROCESSING_QUEUE_MAX,
)
from .lastfm_client import LastFMClient, LastFMTrack
from .deezer_fetch import DeezerClient

if TYPE_CHECKING:
    from .autoplayengine_v3 import AutoplayEngineV3
    from .enrichment_worker import EnrichmentWorker, Priority

LOG = logging.getLogger(__name__)


class FetcherState(Enum):
    """Current state of the Track Fetcher."""
    IDLE = "idle"                     # Waiting for next cycle
    FIRST_RUN = "first_run"           # Scenario A: First run bootstrap
    DAYDREAMING = "daydreaming"       # Scenario B: Genre exploration
    NEW_RELEASES = "new_releases"     # Scenario C: Checking new releases
    PAUSED = "paused"                 # Paused (user sessions active)


class TrackFetcher:
    """
    Track Fetcher (Daydreamer) - Discovers tracks and queues for enrichment.
    
    This is a pure track discovery module. All processing is delegated
    to the EnrichmentWorker via the engine.
    """
    
    def __init__(self, engine: "AutoplayEngineV3"):
        self.engine = engine
        self._verbose = DEFAULT_VERBOSITY
        
        # State
        self._state = FetcherState.IDLE
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        
        # Tracking
        self._fetched_tracks: Set[str] = set()  # Tracks we've already fetched
        self._exploration_tags: List[str] = []  # Tags for genre exploration
        self._current_tag_index = 0
        
        # Timing
        self._last_daydream_time = 0.0
        self._last_new_release_check = 0.0
        self._new_release_continuation = False  # Signal to continue next batch
        
        # Persistence
        self._state_file = Path("cache/music/track_fetcher_state.json")
        self._load_state()
    
    def _vlog(self, level: int, message: str, *args) -> None:
        """Log based on verbosity level."""
        if self._verbose < level:
            return
        log_fn = LOG.info if level == 1 else LOG.debug
        log_fn(message, *args)
    
    # =========================================================================
    # State Persistence
    # =========================================================================
    
    def _load_state(self) -> None:
        """Load state from disk."""
        if self._state_file.exists():
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._fetched_tracks = set(data.get("fetched_tracks", []))
                    self._exploration_tags = data.get("exploration_tags", [])
                    self._current_tag_index = data.get("current_tag_index", 0)
                    self._last_daydream_time = data.get("last_daydream_time", 0.0)
                    self._last_new_release_check = data.get("last_new_release_check", 0.0)
                    self._new_release_continuation = data.get("new_release_continuation", False)
                    self._vlog(1, "🌱 [Fetcher] Loaded state: %d tracks fetched", len(self._fetched_tracks))
            except Exception as e:
                LOG.warning("⚠️ [Fetcher] Failed to load state: %s", str(e)[:100])
    
    def _save_state(self) -> None:
        """Save state to disk."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump({
                    "fetched_tracks": list(self._fetched_tracks),
                    "exploration_tags": self._exploration_tags,
                    "current_tag_index": self._current_tag_index,
                    "last_daydream_time": self._last_daydream_time,
                    "last_new_release_check": self._last_new_release_check,
                    "new_release_continuation": self._new_release_continuation,
                    "saved_at": time.time(),
                }, f)
        except Exception as e:
            LOG.warning("⚠️ [Fetcher] Failed to save state: %s", str(e)[:100])
    
    # =========================================================================
    # Lifecycle
    # =========================================================================
    
    async def start(self) -> None:
        """Start the Track Fetcher."""
        if self._is_running:
            return
        
        self._is_running = True
        self._task = asyncio.create_task(self._main_loop())
        self._vlog(1, "🌱 [Fetcher] Started")
    
    async def stop(self) -> None:
        """Stop the Track Fetcher."""
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        self._save_state()
        self._vlog(1, "🌱 [Fetcher] Stopped")
    
    # =========================================================================
    # Helpers
    # =========================================================================
    
    def _has_active_sessions(self) -> bool:
        """Check if any user sessions are active."""
        if hasattr(self.engine, '_session_manager'):
            return self.engine._session_manager.has_active_sessions()
        return False
    
    def _get_enrichment_worker(self) -> Optional["EnrichmentWorker"]:
        """Get the enrichment worker from engine."""
        return getattr(self.engine, '_enrichment_worker', None)
    
    def _make_track_key(self, artist: str, title: str) -> str:
        """Create normalized track key for deduplication."""
        return f"{artist.lower().strip()}::{title.lower().strip()}"
    
    def _is_first_run(self) -> bool:
        """Check if this is the first run (no enrichment data)."""
        # First run if we haven't fetched many tracks yet
        return len(self._fetched_tracks) < FIRST_RUN_FETCH_COUNT // 2
    
    def _should_check_new_releases(self) -> bool:
        """Check if it's time for a new releases check."""
        if self._new_release_continuation:
            return True
        days_since_check = (time.time() - self._last_new_release_check) / 86400
        return days_since_check >= NEW_RELEASE_CHECK_DAYS
    
    async def _get_enrichment_cache_size(self) -> int:
        """Get the number of tracks in enrichment cache."""
        if hasattr(self.engine, '_cache') and hasattr(self.engine._cache, '_enrichment_cache'):
            return len(self.engine._cache._enrichment_cache)
        return 0
    
    # =========================================================================
    # Main Loop
    # =========================================================================
    
    async def _main_loop(self) -> None:
        """Main fetcher loop."""
        # Initial delay
        await asyncio.sleep(10)
        
        while self._is_running:
            try:
                # Check for active sessions
                if self._has_active_sessions():
                    if self._state != FetcherState.PAUSED:
                        self._state = FetcherState.PAUSED
                        self._vlog(1, "💤 [Fetcher] Paused (active sessions)")
                    await asyncio.sleep(30)
                    continue
                
                # Resume from pause
                if self._state == FetcherState.PAUSED:
                    self._state = FetcherState.IDLE
                    self._vlog(1, "🌱 [Fetcher] Resumed")
                
                # Check enrichment worker queue size
                worker = self._get_enrichment_worker()
                if worker and worker.get_queue_size() >= PROCESSING_QUEUE_MAX:
                    self._vlog(2, "💤 [Fetcher] Worker queue full, waiting...")
                    await asyncio.sleep(60)
                    continue
                
                # Determine what to do
                if self._is_first_run():
                    await self._scenario_first_run()
                elif self._should_check_new_releases():
                    await self._scenario_new_releases()
                else:
                    await self._scenario_daydreaming()
                
                # Save state after each cycle
                self._save_state()
                
                # Wait for next cycle
                await asyncio.sleep(DAYDREAM_INTERVAL_SECONDS)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                LOG.error("❌ [Fetcher] Error: %s", str(e)[:100])
                await asyncio.sleep(60)
    
    # =========================================================================
    # Scenario A: First Run
    # =========================================================================
    
    async def _scenario_first_run(self) -> None:
        """
        Scenario A: First run bootstrap.
        
        Fetch 200 tracks from Last.fm chart.getTopTracks and queue
        for enrichment.
        """
        self._state = FetcherState.FIRST_RUN
        self._vlog(1, "🚀 [Fetcher] First run: Fetching %d tracks from Last.fm", FIRST_RUN_FETCH_COUNT)
        
        try:
            async with LastFMClient() as client:
                tracks = await client.get_top_tracks(limit=FIRST_RUN_FETCH_COUNT)
            
            if not tracks:
                LOG.warning("⚠️ [Fetcher] No tracks from Last.fm, falling back to Deezer")
                # Fallback to Deezer
                async with DeezerClient(max_concurrent=10, timeout=10.0) as client:
                    deezer_tracks = await client.get_charts(limit=FIRST_RUN_FETCH_COUNT)
                    tracks = [
                        LastFMTrack(artist=t.artist, title=t.title)
                        for t in deezer_tracks
                    ]
            
            if not tracks:
                LOG.error("❌ [Fetcher] No tracks available for first run")
                return
            
            # Queue all tracks for enrichment
            added = await self._queue_tracks_for_enrichment(tracks, source="first_run")
            self._vlog(1, "✅ [Fetcher] First run complete: %d/%d tracks queued", added, len(tracks))
            
            # Build exploration tags from top tracks
            await self._build_exploration_tags(tracks[:50])
            
        except Exception as e:
            LOG.error("❌ [Fetcher] First run failed: %s", str(e)[:100])
    
    # =========================================================================
    # Scenario B: Daydreaming
    # =========================================================================
    
    async def _scenario_daydreaming(self) -> None:
        """
        Scenario B: Genre exploration (daydreaming).
        
        Use Last.fm track.getSimilar or tag.getTopTracks to explore
        tracks similar to what we already have.
        """
        self._state = FetcherState.DAYDREAMING
        self._vlog(1, "🌱 [Fetcher] Daydreaming: Exploring new tracks...")
        
        try:
            tracks_to_add: List[LastFMTrack] = []
            
            # Strategy: Alternate between similar tracks and tag exploration
            if self._exploration_tags and self._current_tag_index < len(self._exploration_tags):
                # Tag-based exploration
                tag = self._exploration_tags[self._current_tag_index]
                self._current_tag_index = (self._current_tag_index + 1) % len(self._exploration_tags)
                
                async with LastFMClient() as client:
                    tracks_to_add = await client.get_tag_top_tracks(tag, limit=DAYDREAM_BATCH_SIZE)
                
                self._vlog(2, "🏷️ [Fetcher] Tag exploration '%s': %d tracks", tag, len(tracks_to_add))
            else:
                # Similar track exploration - pick a random track from cache
                sample_tracks = list(self._fetched_tracks)[:10]
                if sample_tracks:
                    # Parse track key back to artist/title
                    sample_key = sample_tracks[0]
                    if "::" in sample_key:
                        artist, title = sample_key.split("::", 1)
                        async with LastFMClient() as client:
                            tracks_to_add = await client.get_similar_tracks(artist, title, limit=DAYDREAM_BATCH_SIZE)
                        self._vlog(2, "🔗 [Fetcher] Similar to '%s - %s': %d tracks", artist, title, len(tracks_to_add))
            
            if not tracks_to_add:
                # Fallback to charts
                async with LastFMClient() as client:
                    tracks_to_add = await client.get_top_tracks(limit=DAYDREAM_BATCH_SIZE)
            
            # Queue for enrichment
            added = await self._queue_tracks_for_enrichment(tracks_to_add, source="daydream")
            self._last_daydream_time = time.time()
            
            self._vlog(1, "🌱 [Fetcher] Daydream complete: %d/%d new tracks queued", added, len(tracks_to_add))
            
        except Exception as e:
            LOG.error("❌ [Fetcher] Daydreaming failed: %s", str(e)[:100])
    
    # =========================================================================
    # Scenario C: New Releases
    # =========================================================================
    
    async def _scenario_new_releases(self) -> None:
        """
        Scenario C: Check for new releases.
        
        Use Deezer editorial releases to find new music.
        """
        self._state = FetcherState.NEW_RELEASES
        self._vlog(1, "🆕 [Fetcher] Checking new releases...")
        
        try:
            async with DeezerClient(max_concurrent=10, timeout=10.0) as client:
                # Deezer editorial releases
                releases = await client.get_new_releases(limit=200)
            
            if not releases:
                LOG.warning("⚠️ [Fetcher] No new releases found")
                self._last_new_release_check = time.time()
                self._new_release_continuation = False
                return
            
            # Convert to LastFMTrack format for consistency
            tracks = [
                LastFMTrack(artist=r.artist, title=r.title)
                for r in releases
                if hasattr(r, 'artist') and hasattr(r, 'title')
            ]
            
            # Queue for enrichment (up to batch size)
            added = await self._queue_tracks_for_enrichment(
                tracks[:DAYDREAM_BATCH_SIZE],
                source="new_releases"
            )
            
            # Check if we need to continue next batch
            if added >= DAYDREAM_BATCH_SIZE:
                self._new_release_continuation = True
                self._vlog(1, "🆕 [Fetcher] New releases: %d added, continuing next batch", added)
            else:
                self._new_release_continuation = False
                self._last_new_release_check = time.time()
                self._vlog(1, "✅ [Fetcher] New releases complete: %d tracks added", added)
            
        except Exception as e:
            LOG.error("❌ [Fetcher] New releases check failed: %s", str(e)[:100])
            self._new_release_continuation = False
    
    # =========================================================================
    # Queue Management
    # =========================================================================
    
    async def _queue_tracks_for_enrichment(
        self,
        tracks: List[LastFMTrack],
        source: str = "unknown",
    ) -> int:
        """
        Queue tracks for enrichment via the EnrichmentWorker.
        
        Filters out duplicates and already-fetched tracks.
        
        Args:
            tracks: List of LastFMTrack objects
            source: Source identifier for logging
        
        Returns:
            Number of tracks successfully queued
        """
        worker = self._get_enrichment_worker()
        if not worker:
            LOG.warning("⚠️ [Fetcher] No enrichment worker available")
            return 0
        
        from .enrichment_worker import Priority
        
        added = 0
        for track in tracks:
            track_key = self._make_track_key(track.artist, track.title)
            
            # Skip if already fetched
            if track_key in self._fetched_tracks:
                continue
            
            # Queue for enrichment
            success = await worker.add_task(
                artist=track.artist,
                title=track.title,
                priority=Priority.DAYDREAM,
                source=source,
            )
            
            if success:
                self._fetched_tracks.add(track_key)
                added += 1
            
            # Stop if we've added enough
            if added >= DAYDREAM_BATCH_SIZE:
                break
        
        return added
    
    async def _build_exploration_tags(self, sample_tracks: List[LastFMTrack]) -> None:
        """
        Build exploration tags from sample tracks.
        
        Uses Last.fm to get top tags for tracks, then uses those
        for future genre exploration.
        """
        # Default exploration tags
        default_tags = ["pop", "rock", "hip-hop", "electronic", "indie", "r&b", "jazz", "classical"]
        
        # TODO: In future, fetch tags from Last.fm track.getTopTags
        # For now, use defaults
        self._exploration_tags = default_tags
        self._current_tag_index = 0
        
        self._vlog(2, "🏷️ [Fetcher] Built exploration tags: %s", ", ".join(self._exploration_tags))
    
    # =========================================================================
    # Public API
    # =========================================================================
    
    def get_state(self) -> Dict[str, Any]:
        """Get current fetcher state."""
        return {
            "state": self._state.value,
            "fetched_count": len(self._fetched_tracks),
            "exploration_tags": self._exploration_tags,
            "last_daydream_time": self._last_daydream_time,
            "last_new_release_check": self._last_new_release_check,
            "new_release_continuation": self._new_release_continuation,
        }
