"""
Buffer Manager - Apple Music-style 5-Track Sliding Buffer for V3 Autoplay Engine.

This module manages the autoplay buffer queue:
- Maintains 5-track sliding window (like Apple Music's "Playing Next")
- P2 priority for buffer enrichment
- Nukes buffer when user adds track to queue
- Variance injection (70% familiar, 30% discovery)

Key Features:
- Buffer refill triggers when count drops below 5
- Buffer nuke clears and rebuilds on user queue add
- Tracks buffer state across sessions
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple, Any

from .config import (
    BUFFER_TARGET_SIZE,
    PRIORITY_BUFFER,
)

if TYPE_CHECKING:
    from .autoplayengine_v3 import AutoplayEngineV3
    from .enrichment_worker import EnrichmentWorker

LOG = logging.getLogger(__name__)


class BufferState(Enum):
    """Current state of the autoplay buffer."""
    EMPTY = "empty"           # No tracks in buffer
    BUILDING = "building"     # Actively adding tracks
    PARTIAL = "partial"       # 1-4 tracks
    FULL = "full"             # 5 tracks (target)
    PAUSED = "paused"         # Temporarily paused


@dataclass
class BufferTrack:
    """A track in the buffer with metadata."""
    artist: str
    title: str
    added_at: float = field(default_factory=time.time)
    is_familiar: bool = True  # True = familiar artist, False = discovery
    similarity_score: float = 0.0  # How similar to current context
    source: str = "recommender"  # Where this recommendation came from
    
    @property
    def track_key(self) -> str:
        return f"{self.artist.lower().strip()}::{self.title.lower().strip()}"
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "artist": self.artist,
            "title": self.title,
            "added_at": self.added_at,
            "is_familiar": self.is_familiar,
            "similarity_score": self.similarity_score,
            "source": self.source,
        }


class BufferManager:
    """
    Apple Music-style 5-track sliding buffer for autoplay.
    
    Like a waiter refilling your cup - when buffer drops below 5, start adding more.
    
    Buffer Behavior:
    | Buffer Size | Action                           |
    | 5 songs     | Full - no action needed          |
    | 4 songs     | Start enriching 1 candidate      |
    | 3 songs     | Enriching 2 candidates           |
    | <3 songs    | Priority enrichment mode         |
    | 0 songs     | User must wait (Quality > Speed) |
    
    Buffer Variance:
    - 70% Familiar: Artist played before OR embedding similarity > 0.85
    - 30% Discovery: New artist with embedding similarity 0.6-0.8
    
    Buffer Nuke:
    - When user adds a track to queue, nuke buffer and rebuild
    - This ensures recommendations stay relevant to user's new direction
    """
    
    def __init__(
        self,
        engine: "AutoplayEngineV3",
        target_size: int = BUFFER_TARGET_SIZE,
        familiar_ratio: float = 0.7,
    ):
        self.engine = engine
        self._target_size = target_size
        self._familiar_ratio = familiar_ratio  # 70% familiar, 30% discovery
        
        # Buffer state
        self._buffer: List[BufferTrack] = []
        self._buffer_lock = asyncio.Lock()
        self._state = BufferState.EMPTY
        
        # Track keys currently in buffer (for deduplication)
        self._buffered_keys: Set[str] = set()
        
        # History tracking for variance calculation
        self._played_artists: Set[str] = set()  # Artists user has heard
        self._discovery_count = 0  # Discovery tracks in current buffer
        
        # Refill task
        self._refill_task: Optional[asyncio.Task] = None
        self._is_running = False
        
        # Stats
        self._stats = {
            "buffer_fills": 0,
            "buffer_nukes": 0,
            "familiar_added": 0,
            "discovery_added": 0,
            "tracks_played": 0,
        }
    
    # =========================================================================
    # Buffer State Management
    # =========================================================================
    
    def _update_state(self) -> None:
        """Update buffer state based on current size."""
        size = len(self._buffer)
        if size == 0:
            self._state = BufferState.EMPTY
        elif size >= self._target_size:
            self._state = BufferState.FULL
        else:
            self._state = BufferState.PARTIAL
    
    async def get_state(self) -> BufferState:
        """Get current buffer state."""
        async with self._buffer_lock:
            self._update_state()
            return self._state
    
    async def get_size(self) -> int:
        """Get current buffer size."""
        async with self._buffer_lock:
            return len(self._buffer)
    
    async def get_buffer(self) -> List[BufferTrack]:
        """Get copy of current buffer."""
        async with self._buffer_lock:
            return list(self._buffer)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get buffer statistics."""
        return {
            **self._stats,
            "buffer_size": len(self._buffer),
            "state": self._state.value,
            "target_size": self._target_size,
            "discovery_ratio": self._discovery_count / max(1, len(self._buffer)),
            "played_artists": len(self._played_artists),
        }
    
    # =========================================================================
    # Buffer Operations
    # =========================================================================
    
    async def add_track(
        self,
        artist: str,
        title: str,
        is_familiar: bool = True,
        similarity_score: float = 0.0,
        source: str = "recommender",
    ) -> bool:
        """
        Add a track to the buffer.
        
        Args:
            artist: Track artist
            title: Track title
            is_familiar: Whether this is a familiar artist (True) or discovery (False)
            similarity_score: Embedding similarity score (0-1)
            source: Where this recommendation came from
            
        Returns:
            True if added, False if duplicate or buffer full
        """
        track = BufferTrack(
            artist=artist.strip(),
            title=title.strip(),
            is_familiar=is_familiar,
            similarity_score=similarity_score,
            source=source,
        )
        
        async with self._buffer_lock:
            # Check for duplicates
            if track.track_key in self._buffered_keys:
                return False
            
            # Check buffer size
            if len(self._buffer) >= self._target_size:
                return False
            
            # Check variance - ensure discovery ratio is maintained
            if not is_familiar:
                max_discovery = int(self._target_size * (1 - self._familiar_ratio))
                if self._discovery_count >= max_discovery:
                    LOG.debug("Buffer: Discovery limit reached, marking as familiar")
                    track.is_familiar = True
                else:
                    self._discovery_count += 1
                    self._stats["discovery_added"] += 1
            else:
                self._stats["familiar_added"] += 1
            
            # Add to buffer
            self._buffer.append(track)
            self._buffered_keys.add(track.track_key)
            self._update_state()
            
            LOG.debug(
                "📥 [Buffer] Added: %s - %s (familiar=%s, sim=%.2f, source=%s)",
                artist, title, is_familiar, similarity_score, source
            )
            
            if self._state == BufferState.FULL:
                self._stats["buffer_fills"] += 1
                LOG.info("✅ [Buffer] Full (%d tracks)", len(self._buffer))
            
            return True
    
    async def get_next_track(self) -> Optional[BufferTrack]:
        """
        Get and remove the next track from buffer.
        
        Returns:
            Next BufferTrack or None if buffer is empty
        """
        async with self._buffer_lock:
            if not self._buffer:
                return None
            
            track = self._buffer.pop(0)
            self._buffered_keys.discard(track.track_key)
            
            # Update discovery count if this was a discovery track
            if not track.is_familiar and self._discovery_count > 0:
                self._discovery_count -= 1
            
            # Track played artists
            self._played_artists.add(track.artist.lower())
            self._stats["tracks_played"] += 1
            
            self._update_state()
            
            LOG.debug(
                "📤 [Buffer] Popped: %s - %s (remaining=%d)",
                track.artist, track.title, len(self._buffer)
            )
            
            return track
    
    async def peek_next_track(self) -> Optional[BufferTrack]:
        """Peek at next track without removing it."""
        async with self._buffer_lock:
            if not self._buffer:
                return None
            return self._buffer[0]
    
    async def nuke_buffer(self, reason: str = "user_queue_add") -> int:
        """
        Clear the buffer completely (buffer nuke).
        
        Called when:
        - User adds a track to queue manually
        - Session ends
        - Major context change detected
        
        Args:
            reason: Why the buffer is being nuked
            
        Returns:
            Number of tracks that were cleared
        """
        async with self._buffer_lock:
            count = len(self._buffer)
            
            if count > 0:
                LOG.info(
                    "💣 [Buffer] NUKE: Clearing %d tracks (reason=%s)",
                    count, reason
                )
                
                self._buffer.clear()
                self._buffered_keys.clear()
                self._discovery_count = 0
                self._state = BufferState.EMPTY
                self._stats["buffer_nukes"] += 1
            
            return count
    
    async def is_track_buffered(self, artist: str, title: str) -> bool:
        """Check if a track is already in the buffer."""
        key = f"{artist.lower().strip()}::{title.lower().strip()}"
        async with self._buffer_lock:
            return key in self._buffered_keys
    
    def is_artist_familiar(self, artist: str) -> bool:
        """Check if user has heard this artist before."""
        return artist.lower() in self._played_artists
    
    def mark_artist_as_played(self, artist: str) -> None:
        """Mark an artist as played (for familiar calculation)."""
        self._played_artists.add(artist.lower())
    
    # =========================================================================
    # Buffer Refill Logic
    # =========================================================================
    
    async def start_refill_loop(self) -> None:
        """Start the background buffer refill task."""
        if self._is_running:
            return
        
        self._is_running = True
        self._refill_task = asyncio.create_task(self._refill_loop())
        LOG.info("🔄 [Buffer] Started refill loop")
    
    async def stop_refill_loop(self) -> None:
        """Stop the background buffer refill task."""
        self._is_running = False
        
        if self._refill_task:
            self._refill_task.cancel()
            try:
                await self._refill_task
            except asyncio.CancelledError:
                pass
        
        LOG.info("🛑 [Buffer] Stopped refill loop")
    
    async def _refill_loop(self) -> None:
        """Background loop that keeps buffer filled."""
        while self._is_running:
            try:
                # Check if refill is needed
                async with self._buffer_lock:
                    current_size = len(self._buffer)
                    needed = self._target_size - current_size
                
                if needed > 0:
                    LOG.debug(
                        "🔄 [Buffer] Refill needed: %d/%d (need %d more)",
                        current_size, self._target_size, needed
                    )
                    
                    # Request recommendations from engine
                    await self._request_refill(needed)
                
                # Wait before next check
                await asyncio.sleep(2)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                LOG.error("❌ [Buffer] Refill loop error: %s", str(e)[:100])
                await asyncio.sleep(5)
    
    async def _request_refill(self, count: int) -> int:
        """
        Request recommendations to fill the buffer.
        
        Args:
            count: Number of tracks needed
            
        Returns:
            Number of tracks added
        """
        if not hasattr(self.engine, '_recommender') or not self.engine._recommender:
            return 0
        
        try:
            # Calculate how many should be discovery vs familiar
            familiar_count = int(count * self._familiar_ratio)
            discovery_count = count - familiar_count
            
            # Get recommendations from the contextual recommender
            # The recommender will return candidates based on current context
            recommendations = await self.engine._recommender.get_recommendations(
                count=count,
                include_discovery=discovery_count > 0,
            )
            
            if not recommendations:
                return 0
            
            added = 0
            for rec in recommendations:
                # Determine if this is familiar or discovery
                is_familiar = self.is_artist_familiar(rec.get("artist", ""))
                
                # If we have similarity score, use threshold
                similarity = rec.get("similarity_score", 0.0)
                if similarity > 0.85:
                    is_familiar = True
                elif similarity < 0.6:
                    is_familiar = False
                
                success = await self.add_track(
                    artist=rec.get("artist", ""),
                    title=rec.get("title", ""),
                    is_familiar=is_familiar,
                    similarity_score=similarity,
                    source="recommender",
                )
                
                if success:
                    added += 1
            
            if added > 0:
                LOG.info(
                    "📥 [Buffer] Refilled %d/%d tracks (buffer=%d/%d)",
                    added, count, len(self._buffer), self._target_size
                )
            
            return added
            
        except Exception as e:
            LOG.error("❌ [Buffer] Refill request failed: %s", str(e)[:100])
            return 0
    
    # =========================================================================
    # User Queue Integration
    # =========================================================================
    
    async def on_user_queue_add(self, artist: str, title: str) -> None:
        """
        Called when user manually adds a track to queue.
        
        This triggers a buffer nuke - the user is signaling they want
        something different, so our pre-buffered recommendations are
        now stale and need to be refreshed based on the new context.
        
        Args:
            artist: Artist of the track user added
            title: Title of the track user added
        """
        # Mark artist as familiar (user explicitly chose them)
        self.mark_artist_as_played(artist)
        
        # Nuke buffer - our predictions are now invalid
        await self.nuke_buffer(reason="user_queue_add")
        
        LOG.info(
            "💣 [Buffer] User added '%s - %s', buffer nuked for fresh recommendations",
            artist, title
        )
    
    async def on_track_skip(self, artist: str, title: str) -> None:
        """
        Called when user skips a track.
        
        This doesn't nuke the buffer but adjusts discovery ratio:
        - If a discovery track was skipped, reduce discovery ratio temporarily
        - If familiar tracks are being skipped, increase discovery
        
        Args:
            artist: Artist of skipped track
            title: Title of skipped track
        """
        # Check if the skipped track was a discovery
        track_key = f"{artist.lower().strip()}::{title.lower().strip()}"
        
        # Find if it was in our buffer (already popped, but we can check familiar status)
        was_discovery = not self.is_artist_familiar(artist)
        
        if was_discovery:
            # User didn't like discovery - be more conservative
            LOG.debug("[Buffer] Discovery track skipped, adjusting ratio")
        
        # Note: We don't nuke on skip - just use skip data for future recommendations
    
    async def on_track_complete(self, artist: str, title: str) -> None:
        """
        Called when user completes a track (didn't skip).
        
        This is positive feedback - the user liked our recommendation.
        
        Args:
            artist: Artist of completed track
            title: Title of completed track
        """
        # Mark artist as familiar (they've heard and liked them)
        self.mark_artist_as_played(artist)
        
        LOG.debug(
            "[Buffer] Track completed: %s - %s (artist now familiar)",
            artist, title
        )
