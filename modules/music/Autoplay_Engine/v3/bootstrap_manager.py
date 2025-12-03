import asyncio
import logging
import json
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Set, Optional, List, Dict, Any, Callable

from .deezer_fetch import DeezerClient
from .lastfm_client import LastFMClient
from .cache_manager import MappingEntry, EnrichmentEntry
from .config import (
    PRIORITY_DAYDREAM,
    MAX_QUEUE_BACKLOG,
    DEFAULT_VERBOSITY,
    FIRST_RUN_FETCH_COUNT,
    DAYDREAM_BATCH_SIZE,
    DAYDREAM_INTERVAL_SECONDS,
    NEW_RELEASE_CHECK_DAYS,
)

if TYPE_CHECKING:
    from .autoplayengine_v3 import AutoplayEngineV3

LOG = logging.getLogger(__name__)


class DaydreamScenario(Enum):
    """Current daydream scenario based on cache state."""
    FIRST_RUN = "first_run"          # Scenario A: Empty cache, fetch 200 from Last.fm
    DAYDREAMING = "daydreaming"      # Scenario B: Explore tracks via Last.fm similar/tags
    NEW_RELEASES = "new_releases"    # Scenario C: Monthly new releases from Deezer
    IDLE = "idle"                    # Cache full, waiting

class BootstrapManager:
    """
    Manages cold-start bootstrapping ("Daydreaming") by fetching popular tracks 
    from Last.fm/Deezer and feeding them into the analysis queue.
    
    Implements three scenarios from v3_reimplementation.md:
    - Scenario A: First Run - Fetch 200 tracks from Last.fm charts
    - Scenario B: Daydreaming - Explore tracks via Last.fm similar/tags  
    - Scenario C: New Releases - Monthly check from Deezer editorial
    
    Key behaviors:
    - Only runs when NO active sessions 
    - Pauses immediately when a user session starts
    - Builds cache proactively for future recommendations
    - Priority 3 (lowest) - never blocks user requests
    """
    
    def __init__(self, engine: "AutoplayEngineV3"):
        self.engine = engine
        self._bootstrapped_tracks: Set[str] = set()
        self._is_running = False
        self._is_paused = False  # Paused when sessions are active
        self._task: Optional[asyncio.Task] = None
        self._bootstrap_limit = FIRST_RUN_FETCH_COUNT  # Stop after bootstrapping this many tracks
        self._verbose = DEFAULT_VERBOSITY
        
        # Scenario tracking
        self._current_scenario = DaydreamScenario.IDLE
        self._exploration_tags: List[str] = ["pop", "rock", "hip-hop", "electronic", "indie", "r&b"]
        self._current_tag_index = 0
        self._last_new_release_check = 0.0
        self._new_release_continuation = False
        
        # Persistence
        self._state_file = Path("cache/music/bootstrap_state.json")
        self._load_state()

    def _vlog(
        self,
        level: int,
        message: str,
        *args,
        log_fn: Optional[Callable[..., None]] = None,
    ) -> None:
        if self._verbose < level:
            return
        if log_fn is None:
            log_fn = LOG.info if level == 1 else LOG.debug
        log_fn(message, *args)

    def _load_state(self):
        """Load bootstrapped tracks from disk."""
        if self._state_file.exists():
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._bootstrapped_tracks = set(data.get("bootstrapped_tracks", []))
                    self._exploration_tags = data.get("exploration_tags", self._exploration_tags)
                    self._current_tag_index = data.get("current_tag_index", 0)
                    self._last_new_release_check = data.get("last_new_release_check", 0.0)
                    self._new_release_continuation = data.get("new_release_continuation", False)
                    self._vlog(1, "🌱 [Bootstrap] Resuming: %d tracks already cached", len(self._bootstrapped_tracks))
            except Exception as e:
                LOG.warning(f"⚠️ [Bootstrap] Failed to load state: {e}")

    def _save_state(self):
        """Save bootstrapped tracks to disk."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump({
                    "bootstrapped_tracks": list(self._bootstrapped_tracks),
                    "exploration_tags": self._exploration_tags,
                    "current_tag_index": self._current_tag_index,
                    "last_new_release_check": self._last_new_release_check,
                    "new_release_continuation": self._new_release_continuation,
                    "saved_at": time.time(),
                }, f)
        except Exception as e:
            LOG.warning(f"⚠️ [Bootstrap] Failed to save state: {e}")

    async def start(self):
        """Start the bootstrap background task."""
        if self._is_running:
            return
        self._is_running = True
        self._task = asyncio.create_task(self._bootstrap_loop())
        self._vlog(1, "🚀 Bootstrap Manager started")

    async def stop(self):
        """Stop the bootstrap background task."""
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._save_state()
        self._vlog(1, "🛑 Bootstrap Manager stopped")
    
    def _has_active_sessions(self) -> bool:
        """Check if any user sessions are active (should pause daydreaming)."""
        if hasattr(self.engine, '_session_manager'):
            return self.engine._session_manager.has_active_sessions()
        return False
    
    def _determine_scenario(self) -> DaydreamScenario:
        """
        Determine which daydream scenario to run based on cache state.
        
        Returns:
            DaydreamScenario enum value
        """
        # Scenario A: First run if we have fewer than half the target tracks
        if len(self._bootstrapped_tracks) < self._bootstrap_limit // 2:
            return DaydreamScenario.FIRST_RUN
        
        # Scenario C: New releases if enough time has passed (or continuation)
        if self._new_release_continuation:
            return DaydreamScenario.NEW_RELEASES
        
        days_since_check = (time.time() - self._last_new_release_check) / 86400
        if days_since_check >= NEW_RELEASE_CHECK_DAYS:
            return DaydreamScenario.NEW_RELEASES
        
        # Scenario B: Normal daydreaming (exploration)
        if len(self._bootstrapped_tracks) < self._bootstrap_limit:
            return DaydreamScenario.DAYDREAMING
        
        # Cache is full, idle mode
        return DaydreamScenario.IDLE

    async def _ensure_cache_entries(self, artist: str, title: str, track) -> bool:
        """
        Create mapping and enrichment cache entries for a Deezer track.
        This is required for queue_analysis to work properly.
        
        Args:
            artist: Track artist
            title: Track title
            track: DeezerTrack object with preview_url, id, duration_ms, etc.
            
        Returns:
            True if entries were created/updated successfully
        """
        track_key = self.engine._make_track_key(artist, title)
        
        try:
            # Step 1: Create/update mapping entry with preview URL
            mapping = await self.engine._cache.get_mapping(artist, title)
            if not mapping:
                mapping = MappingEntry(
                    youtube_id=None,  # No YouTube mapping yet - deferred
                    url=None,
                    title=title,
                    channel_name=artist,
                    duration_ms=track.duration_ms,
                    verified=False,
                    heuristic_score=0.0,
                    timestamp=time.time(),
                )
            
            # Update with Deezer metadata
            mapping.preview_url = track.preview_url
            mapping.preview_duration_ms = track.duration_ms
            mapping.deezer_track_id = track.id
            mapping.preview_fetched_at = time.time()
            
            await self.engine._cache.set_mapping(artist, title, mapping)
            
            # Step 2: Create enrichment entry placeholder if needed
            enrichment = await self.engine._cache.get_enrichment(artist, title)
            if not enrichment:
                enrichment = EnrichmentEntry(
                    tags=[],
                    mood=None,
                    fetched_at=time.time(),
                    bpm=track.bpm if hasattr(track, 'bpm') else None,
                    genres=[],
                )
            
            # Mark as pending analysis
            enrichment.analysis_in_progress = False
            enrichment.analysis_verified = False
            
            await self.engine._cache.set_enrichment(artist, title, enrichment)
            
            self._vlog(
                2, "🌱 [Bootstrap] Created cache entries for %s - %s (preview=%s)",
                artist, title, bool(track.preview_url)
            )
            return True
            
        except Exception as e:
            LOG.warning(f"⚠️ [Bootstrap] Failed to create cache entries for {track_key}: {e}")
            return False

    async def _queue_track_for_analysis(self, artist: str, title: str, track) -> bool:
        """
        Queue a track for analysis with all necessary metadata.
        
        This bypasses the normal queue_analysis flow to directly build a job
        with the Deezer preview URL, since bootstrapped tracks don't come from
        YouTube and don't have YouTube mappings.
        
        Args:
            artist: Track artist
            title: Track title  
            track: DeezerTrack object
            
        Returns:
            True if queued successfully
        """
        if not track.preview_url:
            self._vlog(2, "🚫 [Daydream] No preview URL for %s - %s, skipping", artist, title)
            return False
        
        if not self.engine._preview_fetcher:
            LOG.warning("🚫 [Daydream] Preview fetcher unavailable, cannot queue")
            return False
            
        track_key = self.engine._make_track_key(artist, title)
        
        # Check if already in queue
        if self.engine._is_job_tracked(track_key):
            self._vlog(2, "⏭️ [Daydream] %s already in queue, skipping", track_key)
            return False
        
        # Build job directly with preview URL (bypass normal queue_analysis)
        job: Dict[str, Any] = {
            "track_id": track_key,
            "youtube_url": f"bootstrap:{track.id}",  # Marker URL, not used for download
            "preview_url": track.preview_url,
            "preview_duration_ms": track.duration_ms,
            "deezer_track_id": track.id,
            "priority": PRIORITY_DAYDREAM,  # P3: Daydreaming (lowest priority)
            "attempts": 0,
            "enqueued_at": time.time(),
            "last_error": None,
        }
        
        # Add to queue
        self.engine._analysis_queue.append(job)
        self.engine._analysis_stats["queued"] += 1
        self.engine._analysis_stats["queue_depth"] = len(self.engine._analysis_queue)
        self.engine._persist_analysis_queue_state()
        
        self._vlog(
            2, "✅ [Daydream] Queued %s - %s for analysis (preview_url=%s)",
            artist, title, track.preview_url[:50] + "..." if len(track.preview_url) > 50 else track.preview_url
        )
        return True

    async def _queue_gemini_enrichment_batch(self, tracks) -> int:
        """
        Queue Gemini cultural enrichment for a batch of tracks.
        
        Gemini cultural enrichment only needs artist/title, so we can queue it
        early in parallel with audio analysis. The GeminiService will batch
        requests (max 50 tracks, 5s delay) to minimize API calls.
        
        After queueing, this method flushes the batch and stores results in cache.
        
        Args:
            tracks: List of DeezerTrack objects
            
        Returns:
            Number of tracks successfully enriched
        """
        if not hasattr(self.engine, '_gemini') or not self.engine._gemini:
            self._vlog(2, "⚠️ [Daydream] Gemini service unavailable, skipping enrichment")
            return 0
        
        pending_futures: Dict[str, asyncio.Future] = {}  # track_key -> future
        track_info: Dict[str, tuple] = {}  # track_key -> (artist, title, existing_entry)
        
        for track in tracks:
            try:
                # Check if already enriched
                track_key = self.engine._make_track_key(track.artist, track.title)
                existing = await self.engine._cache.get_enrichment(track.artist, track.title)
                
                # Skip if already has cultural context (tags populated)
                if existing and existing.tags and len(existing.tags) > 0:
                    continue
                
                # Queue for Gemini batch enrichment
                # Uses batching: max 50 tracks, 5s delay before flush
                existing_tags = existing.tags if existing else []
                allow_grounding = self.engine._gemini.can_use_grounding()
                
                # Queue enrichment request - returns a future
                future = await self.engine._gemini.queue_enrichment(
                    artist=track.artist,
                    title=track.title,
                    existing_tags=existing_tags,
                    allow_grounding=allow_grounding,
                )
                pending_futures[track_key] = future
                track_info[track_key] = (track.artist, track.title, existing)
                
            except Exception as e:
                self._vlog(2, "⚠️ [Daydream] Failed to queue enrichment for %s: %s", 
                          track.title, str(e)[:50])
        
        if not pending_futures:
            return 0
        
        self._vlog(1, "📊 [Daydream] Queued %d tracks for Gemini cultural enrichment, flushing...", 
                  len(pending_futures))
        
        # Flush the batch immediately (triggers Gemini API call)
        await self.engine._gemini.flush_enrichment_queue()
        
        # Collect results and store in cache
        enriched_count = 0
        for track_key, future in pending_futures.items():
            try:
                # Get result from future (should already be set after flush)
                result = await asyncio.wait_for(future, timeout=5.0)
                if not result:
                    continue
                
                artist, title, existing_entry = track_info[track_key]
                
                # Merge result into existing entry or create new one
                if existing_entry:
                    entry = existing_entry
                else:
                    from .cache_manager import EnrichmentEntry
                    entry = EnrichmentEntry()
                
                # Update cultural context fields from Gemini result
                if result.get("tags"):
                    entry.tags = [str(t).lower() for t in result["tags"] if str(t).strip()]
                if result.get("activity_affinity"):
                    entry.activity_affinity = result["activity_affinity"]
                if result.get("daypart_affinity"):
                    entry.daypart_affinity = result["daypart_affinity"]
                if result.get("emotional_intensity") is not None:
                    try:
                        entry.emotional_intensity = float(result["emotional_intensity"])
                    except (TypeError, ValueError):
                        pass
                
                # Store updated entry in cache
                await self.engine._cache.set_enrichment(artist, title, entry)
                enriched_count += 1
                
            except asyncio.TimeoutError:
                self._vlog(2, "⚠️ [Daydream] Timeout waiting for enrichment result: %s", track_key)
            except Exception as e:
                self._vlog(2, "⚠️ [Daydream] Failed to store enrichment for %s: %s", 
                          track_key, str(e)[:50])
        
        if enriched_count > 0:
            self._vlog(1, "✅ [Daydream] Cultural enrichment complete: %d/%d tracks enriched", 
                      enriched_count, len(pending_futures))
        
        return enriched_count

    # ==========================================
    # SCENARIO METHODS: Fetch tracks for each scenario
    # ==========================================
    
    async def _scenario_first_run(self) -> List:
        """
        Scenario A: First run - cache is empty or very small.
        Fetch 200 tracks from Last.fm global charts to seed the pool.
        
        Returns:
            List of DeezerTrack objects (verified via Deezer for preview URLs)
        """
        self._vlog(1, "🚀 [Scenario A] First run - fetching %d tracks from Last.fm charts", 
                   FIRST_RUN_FETCH_COUNT)
        
        verified_tracks = []
        
        try:
            # Fetch from Last.fm chart.getTopTracks
            async with LastFMClient(max_concurrent=5, timeout=10.0) as lastfm:
                lastfm_tracks = await lastfm.get_top_tracks(limit=FIRST_RUN_FETCH_COUNT)
            
            if not lastfm_tracks:
                self._vlog(1, "⚠️ [Scenario A] No tracks from Last.fm, falling back to Deezer charts")
                async with DeezerClient(max_concurrent=10, timeout=5.0) as deezer:
                    return await deezer.get_charts(limit=FIRST_RUN_FETCH_COUNT)
            
            self._vlog(1, "📊 [Scenario A] Got %d tracks from Last.fm, verifying via Deezer...", 
                      len(lastfm_tracks))
            
            # Verify each track via Deezer to get preview URLs
            async with DeezerClient(max_concurrent=10, timeout=5.0) as deezer:
                for track_info in lastfm_tracks:
                    try:
                        artist = track_info.get("artist", "")
                        title = track_info.get("title", "")
                        
                        if not artist or not title:
                            continue
                        
                        # Search Deezer for this track
                        results = await deezer.search_track(artist, title)
                        if results and results[0].preview_url:
                            verified_tracks.append(results[0])
                            
                    except Exception as e:
                        self._vlog(2, "⚠️ [Scenario A] Failed to verify %s: %s", 
                                  track_info.get("title", "?"), str(e)[:30])
                        continue
            
            self._vlog(1, "✅ [Scenario A] Verified %d/%d tracks via Deezer", 
                      len(verified_tracks), len(lastfm_tracks))
            
        except Exception as e:
            LOG.error(f"❌ [Scenario A] Error: {e}", exc_info=True)
        
        return verified_tracks
    
    async def _scenario_daydreaming(self) -> List:
        """
        Scenario B: Normal daydreaming - explore based on user listening patterns.
        Uses Last.fm similar tracks or tag-based discovery.
        
        Returns:
            List of DeezerTrack objects
        """
        self._vlog(1, "🌱 [Scenario B] Daydreaming - exploration mode")
        
        verified_tracks = []
        
        try:
            # Get seed tracks from recently played or cached pool
            seed_tracks = await self._get_seed_tracks_for_exploration()
            
            async with LastFMClient(max_concurrent=5, timeout=10.0) as lastfm:
                async with DeezerClient(max_concurrent=10, timeout=5.0) as deezer:
                    
                    # Strategy 1: Similar tracks to seeds
                    if seed_tracks:
                        for seed_artist, seed_title in seed_tracks[:3]:  # Top 3 seeds
                            try:
                                similar = await lastfm.get_similar_tracks(
                                    seed_artist, seed_title, limit=10
                                )
                                for track_info in similar:
                                    artist = track_info.get("artist", "")
                                    title = track_info.get("title", "")
                                    if artist and title:
                                        results = await deezer.search_track(artist, title)
                                        if results and results[0].preview_url:
                                            verified_tracks.append(results[0])
                            except Exception as e:
                                self._vlog(2, "⚠️ [Scenario B] Similar tracks error: %s", str(e)[:30])
                    
                    # Strategy 2: Tag-based discovery
                    if len(verified_tracks) < DAYDREAM_BATCH_SIZE:
                        for tag in self._exploration_tags[:2]:  # Top 2 tags
                            try:
                                tag_tracks = await lastfm.get_tag_top_tracks(tag, limit=10)
                                for track_info in tag_tracks:
                                    artist = track_info.get("artist", "")
                                    title = track_info.get("title", "")
                                    if artist and title:
                                        results = await deezer.search_track(artist, title)
                                        if results and results[0].preview_url:
                                            verified_tracks.append(results[0])
                            except Exception as e:
                                self._vlog(2, "⚠️ [Scenario B] Tag discovery error: %s", str(e)[:30])
            
            # Fallback: Deezer charts if no exploration results
            if not verified_tracks:
                self._vlog(1, "⚠️ [Scenario B] No exploration results, falling back to Deezer charts")
                async with DeezerClient(max_concurrent=10, timeout=5.0) as deezer:
                    verified_tracks = await deezer.get_charts(limit=DAYDREAM_BATCH_SIZE)
            
            self._vlog(1, "✅ [Scenario B] Found %d exploration tracks", len(verified_tracks))
            
        except Exception as e:
            LOG.error(f"❌ [Scenario B] Error: {e}", exc_info=True)
        
        return verified_tracks
    
    async def _scenario_new_releases(self) -> List:
        """
        Scenario C: Monthly new releases check.
        Fetch latest releases from Deezer to keep recommendations fresh.
        
        Returns:
            List of DeezerTrack objects
        """
        self._vlog(1, "🆕 [Scenario C] Checking new releases from Deezer")
        
        verified_tracks = []
        
        try:
            async with DeezerClient(max_concurrent=10, timeout=5.0) as deezer:
                new_releases = await deezer.get_new_releases(limit=50)
                
                # Filter to tracks with preview URLs
                for track in new_releases:
                    if track.preview_url:
                        verified_tracks.append(track)
            
            if verified_tracks:
                self._last_new_release_check = time.time()
                self._new_release_continuation = len(verified_tracks) >= 40  # Continue if many new
                self._save_state()
                
                self._vlog(1, "✅ [Scenario C] Found %d new releases with previews", 
                          len(verified_tracks))
            else:
                self._vlog(1, "⚠️ [Scenario C] No new releases with previews")
                self._new_release_continuation = False
                
        except Exception as e:
            LOG.error(f"❌ [Scenario C] Error: {e}", exc_info=True)
        
        return verified_tracks
    
    async def _get_seed_tracks_for_exploration(self) -> List[tuple]:
        """
        Get seed tracks for exploration (Scenario B).
        Uses recently played tracks or random from cache.
        
        Returns:
            List of (artist, title) tuples
        """
        seeds = []
        
        try:
            # Strategy 1: Recent session tracks
            if hasattr(self.engine, '_session_manager'):
                recent = self.engine._session_manager.get_recent_tracks(limit=5)
                for track_key in recent:
                    if "::" in track_key:
                        parts = track_key.split("::", 1)
                        seeds.append((parts[0], parts[1]))
            
            # Strategy 2: Sample from bootstrapped pool
            if not seeds and self._bootstrapped_tracks:
                import random
                sample = random.sample(
                    list(self._bootstrapped_tracks), 
                    min(5, len(self._bootstrapped_tracks))
                )
                for track_key in sample:
                    if "::" in track_key:
                        parts = track_key.split("::", 1)
                        seeds.append((parts[0], parts[1]))
                        
        except Exception as e:
            self._vlog(2, "⚠️ [Exploration] Failed to get seed tracks: %s", str(e)[:50])
        
        return seeds

    async def _bootstrap_loop(self):
        """
        Main daydream loop - only runs when no active sessions.
        
        Implements Priority 3 background analysis using three scenarios:
        - Scenario A: First run - seed with Last.fm top tracks
        - Scenario B: Daydreaming - explore via similar tracks/tags
        - Scenario C: New releases - monthly Deezer new releases check
        """
        self._vlog(2, "🌱 Starting bootstrap loop (Daydream mode)")
        # Initial delay to let other services settle
        await asyncio.sleep(15)

        while self._is_running:
            try:
                # ========================================
                # CHECK 1: Pause if sessions are active
                # ========================================
                if self._has_active_sessions():
                    if not self._is_paused:
                        self._vlog(1, "💤 [Daydream] Paused (active sessions)")
                        self._is_paused = True
                    await asyncio.sleep(30)  # Check again in 30s
                    continue
                
                # Resume from pause
                if self._is_paused:
                    self._vlog(1, "🌱 [Daydream] Resumed")
                    self._is_paused = False
                
                # ========================================
                # CHECK 2: Have we reached the limit?
                # ========================================
                if len(self._bootstrapped_tracks) >= self._bootstrap_limit:
                    self._vlog(1, "✅ [Daydream] Cache limit reached (%d tracks), idle mode", 
                             self._bootstrap_limit)
                    # Don't stop completely - just sleep longer and check for new charts
                    await asyncio.sleep(3600)  # Check hourly for new charts
                    continue

                # ========================================
                # CHECK 3: Is analysis queue backlogged?
                # ========================================
                stats = self.engine.get_analysis_stats()
                if stats["queue_depth"] > MAX_QUEUE_BACKLOG:
                    # Queue is too full, pause daydreaming entirely
                    self._vlog(2, "💤 [Daydream] Queue backlogged (%d > %d), extended pause...", 
                             stats["queue_depth"], MAX_QUEUE_BACKLOG)
                    await asyncio.sleep(300)  # 5 min extended pause
                    continue
                
                # ========================================
                # CHECK 4: Is analysis queue busy with user requests?
                # ========================================
                if stats["queue_depth"] > 3:
                    # Queue has user requests, yield to them
                    self._vlog(2, "💤 [Daydream] Queue busy (%d items), waiting...", stats["queue_depth"])
                    await asyncio.sleep(30)
                    continue

                # ========================================
                # STEP 1: Determine scenario and fetch tracks
                # ========================================
                scenario = self._determine_scenario()
                self._vlog(1, "🎯 [Daydream] Running scenario: %s", scenario.value)
                
                if scenario == DaydreamScenario.IDLE:
                    self._vlog(1, "💤 [Daydream] Cache full, entering idle mode")
                    await asyncio.sleep(3600)  # Check hourly
                    continue
                
                # Fetch tracks based on scenario
                if scenario == DaydreamScenario.FIRST_RUN:
                    tracks = await self._scenario_first_run()
                elif scenario == DaydreamScenario.NEW_RELEASES:
                    tracks = await self._scenario_new_releases()
                else:  # DAYDREAMING
                    tracks = await self._scenario_daydreaming()
                
                if not tracks:
                    LOG.warning("⚠️ [Daydream] No tracks returned for scenario %s", scenario.value)
                    await asyncio.sleep(300)
                    continue
                
                self._vlog(1, "🌱 [Daydream] Processing %d tracks from %s scenario...", 
                          len(tracks), scenario.value)
                
                # ========================================
                # STEP 1.5: Queue Gemini cultural enrichment for all tracks
                # ========================================
                # Gemini only needs artist/title - queue early for batching
                # This runs in parallel with audio analysis queueing
                await self._queue_gemini_enrichment_batch(tracks)
                
                # ========================================
                # STEP 2: Process tracks for analysis
                # ========================================
                queued_count = 0
                skipped_count = 0
                
                for idx, track in enumerate(tracks):
                    self._vlog(2, "🌱 [Daydream] Processing track %d/%d: %s - %s", 
                              idx + 1, len(tracks), track.artist, track.title)
                    
                    # Stop if sessions became active
                    if self._has_active_sessions():
                        self._vlog(1, "💤 [Daydream] Session started, pausing mid-batch")
                        break
                    
                    if not self._is_running:
                        break
                    
                    # Create normalized key
                    if hasattr(self.engine, "_make_track_key"):
                        track_key = self.engine._make_track_key(track.artist, track.title)
                    else:
                        track_key = f"{track.artist}::{track.title}"
                    
                    # Skip if already processed
                    if track_key in self._bootstrapped_tracks:
                        skipped_count += 1
                        self._vlog(2, "⏭️ [Daydream] Already bootstrapped: %s", track_key)
                        continue
                    
                    # Skip if already fully analyzed in cache
                    if hasattr(self.engine, "_cache") and hasattr(self.engine._cache, "_enrichment_cache"):
                        if track_key in self.engine._cache._enrichment_cache:
                            entry = self.engine._cache._enrichment_cache[track_key]
                            if entry.analysis_verified:
                                self._bootstrapped_tracks.add(track_key)
                                skipped_count += 1
                                continue
                    
                    # ========================================
                    # STEP 3: Queue using direct method (bypasses cache lookup)
                    # ========================================
                    # This creates cache entries AND queues in one step
                    await self._ensure_cache_entries(track.artist, track.title, track)
                    success = await self._queue_track_for_analysis(track.artist, track.title, track)
                    
                    # Always track it for recommendation pool
                    self._bootstrapped_tracks.add(track_key)
                    
                    if success:
                        queued_count += 1
                        self._vlog(2, "🌱 [Daydream] Queued: %s - %s", track.artist, track.title)
                    else:
                        self._vlog(2, "⚠️ [Daydream] Failed to queue: %s - %s", track.artist, track.title)
                    
                    # Rate limit: max 5 per batch to not overwhelm workers
                    if queued_count >= 5:
                        break
                
                # ========================================
                # STEP 4: Summary and save
                # ========================================
                self._vlog(
                    1, "🌱 [Daydream] Batch complete: queued=%d, skipped=%d, total=%d/%d",
                    queued_count,
                    skipped_count,
                    len(self._bootstrapped_tracks),
                    self._bootstrap_limit,
                )
                
                self._save_state()
                
                # Sleep between batches
                if queued_count == 0:
                    await asyncio.sleep(300)  # 5 min if nothing new
                else:
                    await asyncio.sleep(60)  # 1 min between active batches

            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.error(f"❌ [Daydream] Error: {e}", exc_info=True)
                await asyncio.sleep(60)

