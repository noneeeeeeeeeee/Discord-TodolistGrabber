import asyncio
import logging
import json
from pathlib import Path
from typing import TYPE_CHECKING, Set, Optional, List

from .deezer_fetch import DeezerClient

if TYPE_CHECKING:
    from .autoplayengine_v3 import AutoplayEngineV3

LOG = logging.getLogger(__name__)

class BootstrapManager:
    """
    Manages cold-start bootstrapping by fetching popular tracks from Deezer
    and feeding them into the analysis queue.
    """
    
    def __init__(self, engine: "AutoplayEngineV3"):
        self.engine = engine
        self._bootstrapped_tracks: Set[str] = set()
        self._is_running = False
        self._task: Optional[asyncio.Task] = None
        self._bootstrap_limit = 200  # Stop after bootstrapping this many tracks
        
        # Persistence
        self._state_file = Path("cache/music/bootstrap_state.json")
        self._load_state()

    def _load_state(self):
        """Load bootstrapped tracks from disk."""
        if self._state_file.exists():
            try:
                with open(self._state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._bootstrapped_tracks = set(data.get("bootstrapped_tracks", []))
                    LOG.info(f"🌱 [Bootstrap] Resuming from state: {len(self._bootstrapped_tracks)} tracks already processed")
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
        LOG.info("🚀 Bootstrap Manager started")

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
        LOG.info("🛑 Bootstrap Manager stopped")

    async def _bootstrap_loop(self):
        """Main loop for fetching and queuing chart tracks."""
        LOG.info("🌱 Starting bootstrap loop (Deezer Charts)")
        # Initial delay to let other services settle
        await asyncio.sleep(15)

        while self._is_running:
            try:
                # Check if we've reached the limit
                if len(self._bootstrapped_tracks) >= self._bootstrap_limit:
                    LOG.info("✅ Bootstrap limit reached, stopping manager")
                    self._is_running = False
                    break

                # Check if analysis queue is busy
                stats = self.engine.get_analysis_stats()
                if stats["queue_depth"] > 5:
                    await asyncio.sleep(30)
                    continue

                # Fetch charts from Deezer
                async with DeezerClient() as client:
                    tracks = await client.get_charts(limit=50)
                
                queued_count = 0
                for track in tracks:
                    if not self._is_running:
                        break
                    
                    # Create normalized key (accessing private method from engine)
                    # Assuming standard format if method not available
                    if hasattr(self.engine, "_make_track_key"):
                        track_key = self.engine._make_track_key(track.artist, track.title)
                    else:
                        track_key = f"{track.artist}::{track.title}"
                    
                    # Skip if already processed by us
                    if track_key in self._bootstrapped_tracks:
                        continue
                        
                    # Check if already analyzed in engine cache
                    # We access the cache directly to avoid overhead
                    if hasattr(self.engine, "_cache") and hasattr(self.engine._cache, "_enrichment_cache"):
                        if track_key in self.engine._cache._enrichment_cache:
                            entry = self.engine._cache._enrichment_cache[track_key]
                            # If already verified, skip
                            if entry.analysis_verified:
                                self._bootstrapped_tracks.add(track_key)
                                continue

                    # Ensure preview metadata is cached so queue_analysis can find it
                    if track.preview_url:
                        await self.engine.ensure_preview_metadata(
                            track.artist, 
                            track.title,
                            expected_duration_ms=track.duration_ms
                        )
                    
                    # Queue for analysis
                    # We pass a dummy YouTube URL because queue_analysis requires it.
                    # The worker will prefer the preview_url from the mapping.
                    dummy_url = f"https://www.youtube.com/watch?v=bootstrap_{track.id}"
                    
                    success = self.engine.queue_analysis(track_key, dummy_url)
                    
                    if success:
                        self._bootstrapped_tracks.add(track_key)
                        queued_count += 1
                        LOG.debug(f"🌱 Bootstrapped: {track.artist} - {track.title}")
                        
                        # Rate limit: don't flood the queue
                        if queued_count >= 5: 
                            break
                
                # Save state after each batch
                self._save_state()
                
                if queued_count == 0:
                    # No new tracks found or all skipped
                    await asyncio.sleep(300) # Sleep longer
                else:
                    await asyncio.sleep(10) # Short sleep between batches

            except Exception as e:
                LOG.error(f"❌ Bootstrap error: {e}")
                await asyncio.sleep(60)

