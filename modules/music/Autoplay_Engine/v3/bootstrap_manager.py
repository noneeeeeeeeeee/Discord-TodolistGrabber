import asyncio
import logging
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Set, Optional, List, Dict, Any, Callable

from .deezer_fetch import DeezerClient
from .cache_manager import MappingEntry, EnrichmentEntry
from .config import PRIORITY_DAYDREAM, MAX_QUEUE_BACKLOG, DEFAULT_VERBOSITY

if TYPE_CHECKING:
    from .autoplayengine_v3 import AutoplayEngineV3

LOG = logging.getLogger(__name__)

class BootstrapManager:
    """
    Manages cold-start bootstrapping ("Daydreaming") by fetching popular tracks 
    from Last.fm/Deezer and feeding them into the analysis queue.
    
    Key behaviors:
    - Only runs when NO active sessions 
    - Pauses immediately when a user session starts
    - Actually analyzes tracks (downloads preview, runs EfficientAT)
    - Builds cache proactively for future recommendations
    - Priority 3 (lowest) - never blocks user requests
    """
    
    def __init__(self, engine: "AutoplayEngineV3"):
        self.engine = engine
        self._bootstrapped_tracks: Set[str] = set()
        self._is_running = False
        self._is_paused = False  # Paused when sessions are active
        self._task: Optional[asyncio.Task] = None
        self._bootstrap_limit = 200  # Stop after bootstrapping this many tracks
        self._verbose = DEFAULT_VERBOSITY
        
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
                    self._vlog(1, "🌱 [Bootstrap] Resuming: %d tracks already cached", len(self._bootstrapped_tracks))
            except Exception as e:
                LOG.warning(f"⚠️ [Bootstrap] Failed to load state: {e}")

    def _save_state(self):
        """Save bootstrapped tracks to disk."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump({
                    "bootstrapped_tracks": list(self._bootstrapped_tracks)
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

    async def _bootstrap_loop(self):
        """
        Main daydream loop - only runs when no active sessions.
        
        This implements the "Priority 3" background analysis:
        - Fetches tracks from Deezer charts / Last.fm trending
        - Verifies via Deezer and gets preview URL
        - Queues for EfficientAT analysis
        - Does NOT resolve YouTube (deferred until song is chosen)
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
                # STEP 1: Fetch chart tracks from Deezer
                # ========================================
                self._vlog(2, "🌱 [Daydream] Fetching chart tracks...")
                async with DeezerClient(max_concurrent=10, timeout=5.0) as client:
                    tracks = await client.get_charts(limit=50)
                
                if not tracks:
                    LOG.warning("⚠️ [Daydream] No chart tracks returned")
                    await asyncio.sleep(300)
                    continue
                
                self._vlog(1, "🌱 [Daydream] Processing %d chart tracks...", len(tracks))
                
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

