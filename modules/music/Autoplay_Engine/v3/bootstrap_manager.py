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
        
        # Scenario A: First run protection (only runs ONCE per boot)
        self._first_run_triggered = False  # Set True after Scenario A runs
        self._first_run_queued_count = 0   # How many tracks were queued in first run
        
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
                    # First run tracking (reset on new boot if not enough tracks)
                    self._first_run_triggered = data.get("first_run_triggered", False)
                    self._first_run_queued_count = data.get("first_run_queued_count", 0)
                    self._vlog(1, "🌱 [Bootstrap] Resuming: %d tracks cached, first_run_triggered=%s", 
                              len(self._bootstrapped_tracks), self._first_run_triggered)
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
                    "first_run_triggered": self._first_run_triggered,
                    "first_run_queued_count": self._first_run_queued_count,
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
        # Scenario A: First run - only runs ONCE per boot, and only if needed
        # Uses ENRICHED count (analysis_verified=True) not just queued count
        if not self._first_run_triggered:
            # Check how many tracks have been FULLY enriched (Gemini + audio analysis)
            enriched_count = 0
            if hasattr(self.engine, '_cache') and hasattr(self.engine._cache, 'get_enriched_count'):
                enriched_count = self.engine._cache.get_enriched_count()
            
            # Also count tracks with Gemini enrichment (tags populated)
            gemini_count = 0
            if hasattr(self.engine, '_cache') and hasattr(self.engine._cache, 'get_gemini_enriched_count'):
                gemini_count = self.engine._cache.get_gemini_enriched_count()
            
            # Use the higher of the two counts
            actual_enriched = max(enriched_count, gemini_count)
            
            if actual_enriched < self._bootstrap_limit:
                self._vlog(1, "📊 [Scenario Check] First run needed: enriched=%d (audio=%d, gemini=%d), target=%d",
                          actual_enriched, enriched_count, gemini_count, self._bootstrap_limit)
                return DaydreamScenario.FIRST_RUN
            else:
                # Already have enough ENRICHED tracks, mark first run as complete
                self._first_run_triggered = True
                self._vlog(1, "✅ [Scenario Check] First run skipped: already have %d enriched tracks", actual_enriched)
        
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
        Queue a track for enrichment using the EnrichmentWorker.
        
        The EnrichmentWorker handles the full pipeline:
        1. Deezer Resolution (search + track details for BPM/gain)
        2. Gemini Cultural Enrichment (tags, mood, activity)
        3. Audio Analysis (Librosa + EfficientAT for embeddings)
        
        This ensures consistent processing and proper Gemini batching.
        
        Args:
            artist: Track artist
            title: Track title  
            track: DeezerTrack object (may have preview_url already)
            
        Returns:
            True if queued successfully
        """
        if not hasattr(self.engine, '_enrichment_worker') or not self.engine._enrichment_worker:
            LOG.warning("🚫 [Daydream] EnrichmentWorker unavailable, cannot queue")
            return False
        
        track_key = self.engine._make_track_key(artist, title)
        
        # Use EnrichmentWorker to handle full pipeline
        # Priority is DAYDREAM (P3) for background processing
        from .enrichment_worker import Priority
        success = await self.engine._enrichment_worker.add_task(
            artist=artist,
            title=title,
            priority=Priority.DAYDREAM,
            source="bootstrap",
        )
        
        if success:
            self._vlog(2, "✅ [Daydream] Queued %s - %s via EnrichmentWorker", artist, title)
        else:
            self._vlog(2, "⏭️ [Daydream] %s already queued or enriched", track_key)
        
        return success

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
        
        Uses 2-phase resolution:
        - Phase 1: Direct Deezer search for each Last.fm track
        - Phase 2: Batch Gemini resolver for failed tracks (max 50)
        
        Returns:
            List of DeezerTrack objects (verified via Deezer for preview URLs)
        """
        self._vlog(1, "🚀 [Scenario A] First run - fetching %d tracks from Last.fm charts", 
                   FIRST_RUN_FETCH_COUNT)
        
        verified_tracks = []
        failed_tracks: List[Dict[str, str]] = []
        
        try:
            # Fetch from Last.fm chart.getTopTracks
            async with LastFMClient(max_concurrent=5, timeout=10.0) as lastfm:
                lastfm_tracks = await lastfm.get_top_tracks(limit=FIRST_RUN_FETCH_COUNT)
            
            if not lastfm_tracks:
                self._vlog(1, "⚠️ [Scenario A] No tracks from Last.fm, falling back to Deezer charts")
                async with DeezerClient(max_concurrent=10, timeout=5.0) as deezer:
                    return await deezer.get_charts(limit=FIRST_RUN_FETCH_COUNT)
            
            self._vlog(1, "📊 [Scenario A] Got %d tracks from Last.fm, Phase 1: Direct Deezer search...", 
                      len(lastfm_tracks))
            
            # Phase 1: Direct Deezer search for each track
            async with DeezerClient(max_concurrent=10, timeout=5.0) as deezer:
                for track_info in lastfm_tracks:
                    try:
                        artist = track_info.artist
                        title = track_info.title
                        
                        if not artist or not title:
                            continue
                        
                        # Search Deezer for this track
                        results = await deezer.search_track(f"{artist} {title}")
                        if results and results[0].preview_url:
                            verified_tracks.append(results[0])
                        else:
                            # Track to failed list for Phase 2
                            failed_tracks.append({"artist": artist, "title": title})
                            
                    except Exception as e:
                        failed_tracks.append({"artist": getattr(track_info, 'artist', ''), 
                                            "title": getattr(track_info, 'title', '')})
            
            self._vlog(1, "📊 [Scenario A] Phase 1 complete: %d verified, %d failed", 
                      len(verified_tracks), len(failed_tracks))
            
            # Phase 2: Use batch resolver for failed tracks (with Gemini fallback)
            if failed_tracks and hasattr(self.engine, '_track_resolver') and self.engine._track_resolver:
                self._vlog(1, "🔄 [Scenario A] Phase 2: Batch resolving %d failed tracks...", 
                          len(failed_tracks))
                
                batch_result = await self.engine._track_resolver.resolve_batch_to_deezer(
                    failed_tracks,
                    source="lastfm",
                    max_gemini_batch=50,
                )
                
                # Convert resolved tracks to DeezerTrack-like objects
                if batch_result.get("resolved"):
                    from dataclasses import dataclass
                    
                    @dataclass
                    class DeezerTrackProxy:
                        id: int
                        artist: str
                        title: str
                        preview_url: str
                        duration_ms: int
                        
                    for item in batch_result["resolved"]:
                        if item.get("preview_url"):
                            proxy = DeezerTrackProxy(
                                id=item.get("deezer_id", 0),
                                artist=item["artist"],
                                title=item["title"],
                                preview_url=item["preview_url"],
                                duration_ms=item.get("duration_ms", 0),
                            )
                            verified_tracks.append(proxy)
                
                stats = batch_result.get("stats", {})
                self._vlog(1, "✅ [Scenario A] Phase 2 complete: +%d tracks (P1=%d, P2=%d, failed=%d)", 
                          stats.get("phase1_resolved", 0) + stats.get("phase2_resolved", 0),
                          stats.get("phase1_resolved", 0),
                          stats.get("phase2_resolved", 0),
                          stats.get("total_failed", 0))
            
            self._vlog(1, "✅ [Scenario A] Total verified: %d/%d tracks via Deezer", 
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
        failed_tracks: List[Dict[str, str]] = []
        
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
                                    artist = track_info.artist
                                    title = track_info.title
                                    if artist and title:
                                        results = await deezer.search_track(f"{artist} {title}")
                                        if results and results[0].preview_url:
                                            verified_tracks.append(results[0])
                                        else:
                                            failed_tracks.append({"artist": artist, "title": title})
                            except Exception as e:
                                self._vlog(2, "⚠️ [Scenario B] Similar tracks error: %s", str(e)[:30])
                    
                    # Strategy 2: Tag-based discovery
                    if len(verified_tracks) < DAYDREAM_BATCH_SIZE:
                        for tag in self._exploration_tags[:2]:  # Top 2 tags
                            try:
                                tag_tracks = await lastfm.get_tag_top_tracks(tag, limit=10)
                                for track_info in tag_tracks:
                                    artist = track_info.artist
                                    title = track_info.title
                                    if artist and title:
                                        results = await deezer.search_track(f"{artist} {title}")
                                        if results and results[0].preview_url:
                                            verified_tracks.append(results[0])
                                        else:
                                            failed_tracks.append({"artist": artist, "title": title})
                            except Exception as e:
                                self._vlog(2, "⚠️ [Scenario B] Tag discovery error: %s", str(e)[:30])
            
            self._vlog(1, "📊 [Scenario B] Phase 1: %d verified, %d failed", 
                      len(verified_tracks), len(failed_tracks))
            
            # Phase 2: Batch resolve failed tracks
            if failed_tracks and hasattr(self.engine, '_track_resolver') and self.engine._track_resolver:
                batch_result = await self.engine._track_resolver.resolve_batch_to_deezer(
                    failed_tracks[:50],  # Limit to 50 for Gemini batch
                    source="lastfm",
                    max_gemini_batch=50,
                )
                
                if batch_result.get("resolved"):
                    from dataclasses import dataclass
                    
                    @dataclass
                    class DeezerTrackProxy:
                        id: int
                        artist: str
                        title: str
                        preview_url: str
                        duration_ms: int
                        
                    for item in batch_result["resolved"]:
                        if item.get("preview_url"):
                            proxy = DeezerTrackProxy(
                                id=item.get("deezer_id", 0),
                                artist=item["artist"],
                                title=item["title"],
                                preview_url=item["preview_url"],
                                duration_ms=item.get("duration_ms", 0),
                            )
                            verified_tracks.append(proxy)
                
                stats = batch_result.get("stats", {})
                self._vlog(1, "✅ [Scenario B] Phase 2: +%d recovered (P1=%d, P2=%d)", 
                          stats.get("phase1_resolved", 0) + stats.get("phase2_resolved", 0),
                          stats.get("phase1_resolved", 0),
                          stats.get("phase2_resolved", 0))
            
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
        
        V3 Improvements:
        - Fetch max releases per artist (avoid artist flooding)
        - Deduplicate across all artists
        - Fill remaining slots with Scenario B tracks
        
        Returns:
            List of DeezerTrack objects
        """
        self._vlog(1, "🆕 [Scenario C] Checking new releases from Deezer")
        
        verified_tracks = []
        seen_keys: set = set()  # For deduplication
        max_per_artist = 3  # Max tracks per artist to avoid flooding
        artist_counts: dict = {}  # Track count per artist
        
        try:
            async with DeezerClient(max_concurrent=10, timeout=5.0) as deezer:
                new_releases = await deezer.get_new_releases(limit=100)  # Fetch more for filtering
                
                # Filter and deduplicate
                for track in new_releases:
                    if not track.preview_url:
                        continue
                    
                    # Deduplication key
                    track_key = f"{track.artist.lower().strip()}::{track.title.lower().strip()}"
                    if track_key in seen_keys:
                        continue
                    if track_key in self._bootstrapped_tracks:
                        continue
                    
                    # Artist limit check
                    artist_lower = track.artist.lower().strip()
                    artist_count = artist_counts.get(artist_lower, 0)
                    if artist_count >= max_per_artist:
                        self._vlog(2, "⏭️ [Scenario C] Artist limit reached: %s", track.artist)
                        continue
                    
                    # Add track
                    verified_tracks.append(track)
                    seen_keys.add(track_key)
                    artist_counts[artist_lower] = artist_count + 1
                    
                    # Stop if we have enough
                    if len(verified_tracks) >= DAYDREAM_BATCH_SIZE:
                        break
            
            self._vlog(1, "🆕 [Scenario C] Found %d unique new releases (from %d artists)", 
                      len(verified_tracks), len(artist_counts))
            
            # Fill remaining with Scenario B (daydream) tracks
            if len(verified_tracks) < DAYDREAM_BATCH_SIZE:
                remaining = DAYDREAM_BATCH_SIZE - len(verified_tracks)
                self._vlog(1, "🔄 [Scenario C] Filling %d remaining slots with exploration tracks", remaining)
                
                # Get exploration tracks from Scenario B logic
                exploration_tracks = await self._get_exploration_tracks_for_fill(remaining, seen_keys)
                verified_tracks.extend(exploration_tracks)
                
                self._vlog(1, "✅ [Scenario C] Total after fill: %d tracks", len(verified_tracks))
            
            if verified_tracks:
                self._last_new_release_check = time.time()
                self._new_release_continuation = len(verified_tracks) >= 40
                self._save_state()
            else:
                self._vlog(1, "⚠️ [Scenario C] No new releases found")
                self._new_release_continuation = False
                
        except Exception as e:
            LOG.error(f"❌ [Scenario C] Error: {e}", exc_info=True)
        
        return verified_tracks
    
    async def _get_exploration_tracks_for_fill(self, count: int, exclude_keys: set) -> List:
        """
        Get exploration tracks to fill remaining slots (used by Scenario C).
        
        Args:
            count: Number of tracks to fetch
            exclude_keys: Track keys to exclude (already in batch)
            
        Returns:
            List of DeezerTrack objects
        """
        verified_tracks = []
        
        try:
            seed_tracks = await self._get_seed_tracks_for_exploration()
            
            async with LastFMClient(max_concurrent=5, timeout=10.0) as lastfm:
                async with DeezerClient(max_concurrent=10, timeout=5.0) as deezer:
                    
                    # Similar tracks to seeds
                    if seed_tracks:
                        for seed_artist, seed_title in seed_tracks[:2]:
                            if len(verified_tracks) >= count:
                                break
                            try:
                                similar = await lastfm.get_similar_tracks(
                                    seed_artist, seed_title, limit=10
                                )
                                for track_info in similar:
                                    artist = track_info.artist
                                    title = track_info.title
                                    track_key = f"{artist.lower().strip()}::{title.lower().strip()}"
                                    
                                    if track_key in exclude_keys:
                                        continue
                                    if track_key in self._bootstrapped_tracks:
                                        continue
                                    
                                    if artist and title:
                                        results = await deezer.search_track(artist, title)
                                        if results and results[0].preview_url:
                                            verified_tracks.append(results[0])
                                            exclude_keys.add(track_key)
                                            
                                            if len(verified_tracks) >= count:
                                                break
                            except Exception:
                                continue
                    
                    # Tag-based fallback
                    if len(verified_tracks) < count:
                        for tag in self._exploration_tags[:2]:
                            if len(verified_tracks) >= count:
                                break
                            try:
                                tag_tracks = await lastfm.get_tag_top_tracks(tag, limit=10)
                                for track_info in tag_tracks:
                                    artist = track_info.artist
                                    title = track_info.title
                                    track_key = f"{artist.lower().strip()}::{title.lower().strip()}"
                                    
                                    if track_key in exclude_keys:
                                        continue
                                    if track_key in self._bootstrapped_tracks:
                                        continue
                                    
                                    if artist and title:
                                        results = await deezer.search_track(artist, title)
                                        if results and results[0].preview_url:
                                            verified_tracks.append(results[0])
                                            exclude_keys.add(track_key)
                                            
                                            if len(verified_tracks) >= count:
                                                break
                            except Exception:
                                continue
                                
        except Exception as e:
            self._vlog(2, "⚠️ [Scenario C] Fill error: %s", str(e)[:50])
        
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
                # STEP 2: Process tracks for enrichment
                # ========================================
                # NOTE: Gemini enrichment is handled by EnrichmentWorker
                # The worker pipeline is: Deezer → Gemini → Audio Analysis
                # We just need to queue tracks for the worker
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
                    # Queue via EnrichmentWorker (handles Deezer → Gemini → Audio)
                    success = await self._queue_track_for_analysis(track.artist, track.title, track)
                    
                    if success:
                        queued_count += 1
                        # Only track successfully queued tracks
                        self._bootstrapped_tracks.add(track_key)
                        self._vlog(2, "🌱 [Daydream] Queued: %s - %s", track.artist, track.title)
                    else:
                        # Don't add to bootstrapped_tracks - allow retry next time
                        skipped_count += 1
                        self._vlog(2, "⚠️ [Daydream] Failed to queue: %s - %s", track.artist, track.title)
                    
                    # Rate limit based on scenario
                    # FIRST_RUN (Scenario A): No limit - queue all 200 tracks to bootstrap cache
                    if scenario != DaydreamScenario.FIRST_RUN:
                        if queued_count >= DAYDREAM_BATCH_SIZE:
                            self._vlog(1, "🌱 [Daydream] Batch limit reached (%d tracks)", queued_count)
                            break
                
                # ========================================
                # STEP 4: Summary and save
                # ========================================
                # Mark first run as complete (only runs once per boot)
                if scenario == DaydreamScenario.FIRST_RUN and queued_count > 0:
                    self._first_run_triggered = True
                    self._vlog(1, "🚀 [Daydream] FIRST RUN COMPLETE: Queued %d tracks for bootstrap", queued_count)
                
                self._vlog(
                    1, "🌱 [Daydream] Batch complete: scenario=%s, queued=%d, skipped=%d, total=%d/%d",
                    scenario.value,
                    queued_count,
                    skipped_count,
                    len(self._bootstrapped_tracks),
                    self._bootstrap_limit,
                )
                
                self._save_state()
                
                # Sleep between batches - timing depends on enrichment progress
                # Get actual enriched count (not just queued)
                enriched_count = 0
                if hasattr(self.engine, '_cache') and hasattr(self.engine._cache, 'get_enriched_count'):
                    enriched_count = self.engine._cache.get_enriched_count()
                    
                if enriched_count < self._bootstrap_limit:
                    # Still bootstrapping - use fast intervals
                    if scenario == DaydreamScenario.FIRST_RUN:
                        self._vlog(2, "🚀 [Daydream] Bootstrap in progress (%d/%d enriched), quick pause...", 
                                  enriched_count, self._bootstrap_limit)
                        await asyncio.sleep(5)  # Quick 5s pause between first-run batches
                    elif queued_count == 0:
                        self._vlog(2, "💤 [Daydream] Nothing new, waiting...")
                        await asyncio.sleep(60)  # 1 min if nothing new (faster during bootstrap)
                    else:
                        self._vlog(2, "🌱 [Daydream] Batch processing...")
                        await asyncio.sleep(30)  # 30s between active batches during bootstrap
                else:
                    # Bootstrap complete - use normal 30-min intervals
                    self._vlog(1, "✅ [Daydream] Bootstrap complete (%d enriched), using 30-min intervals", 
                              enriched_count)
                    await asyncio.sleep(DAYDREAM_INTERVAL_SECONDS)  # 30 minutes

            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.error(f"❌ [Daydream] Error: {e}", exc_info=True)
                await asyncio.sleep(60)

