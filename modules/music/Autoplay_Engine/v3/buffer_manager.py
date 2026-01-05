"""
V3 Autoplay Engine - Buffer Manager

Manages the 5-song recommendation buffer (Apple Music style):
- Pre-fetches next 5 songs for seamless playback
- Triggers context reanalysis on skip patterns
- Replaces buffer on context shifts
- Coordinates with recommender for refills

Apple Music-Style "Testing the Waters":
- Buffer contains mix of SAFE picks (high confidence) and EXPLORATORY tracks
- Safe picks: Based on proven preferences, transitions, similar to likes
- Exploratory: Diverse picks testing new genres/artists/moods
- Buffer composition adapts based on session state:
  - COLD: 4 safe, 1 exploratory (cautious exploration)
  - WARM: 3 safe, 2 exploratory (balanced)
  - HOT: 2 safe, 3 exploratory (aggressive discovery)

The buffer sits between the recommender and the playback queue,
providing a lookahead window for smoother user experience.
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .constants import EventType, SessionState, V3Config
from .event_bus import EventBus, EventPayload
from .mappings import SongIdentifier
from .recommender import Recommendation, Recommender, get_recommender
from .session_manager import SessionManager, get_session_manager
from .song_analyzer import AnalysisPriority, SongAnalyzer, get_song_analyzer

logger = logging.getLogger(__name__)


class BufferSlotType(Enum):
    """
    Type of buffer slot - determines recommendation strategy.
    
    Apple Music-style buffer composition:
    - SAFE: High confidence picks based on proven preferences
    - EXPLORATORY: "Testing the waters" picks to discover new music
    """
    SAFE = "safe"           # Based on liked songs, good transitions, similar artists
    EXPLORATORY = "exploratory"  # New genres, unfamiliar artists, mood shifts


# Buffer composition by session state (safe_count, exploratory_count)
# Tuple: (safe_slots, exploratory_slots) out of 5 total
BUFFER_COMPOSITION = {
    SessionState.COLD: (4, 1),   # Cautious: mostly safe, one exploration
    SessionState.WARM: (3, 2),   # Balanced: mix of both
    SessionState.HOT: (2, 3),    # Adventurous: more exploration
    SessionState.EXTENDED: (2, 3),  # Same as HOT for extended sessions
}


@dataclass
class BufferedSong:
    """A song in the buffer."""
    identifier: SongIdentifier
    recommendation: Recommendation
    added_at: float
    position: int
    slot_type: BufferSlotType = BufferSlotType.SAFE  # What type of pick this is
    
    @property
    def song_id(self) -> Optional[str]:
        return self.identifier.primary_id
    
    @property
    def is_exploratory(self) -> bool:
        """Check if this is an exploratory pick."""
        return self.slot_type == BufferSlotType.EXPLORATORY


class SessionBuffer:
    """Buffer for a single session with Apple Music-style slot management."""
    
    def __init__(self, session_id: str, size: int = 5):
        """
        Initialize session buffer.
        
        Args:
            session_id: Session identifier
            size: Buffer size (default 5)
        """
        self.session_id = session_id
        self.size = size
        self._buffer: deque[BufferedSong] = deque(maxlen=size)
        
        # Track IDs separately for different purposes:
        self._buffered_ids: set[str] = set()  # Anti-duplicate tracking within buffer
        self._played_ids: set[str] = set()     # Confirmed played songs only
        
        self._position_counter = 0
        self._last_refill = 0.0
        self._refill_lock = asyncio.Lock()
        
        # Track exploratory song outcomes for adaptation
        self._exploratory_outcomes: list[bool] = []  # True = completed, False = skipped
    
    def add(
        self,
        recommendation: Recommendation,
        slot_type: BufferSlotType = BufferSlotType.SAFE
    ) -> BufferedSong:
        """Add a recommendation to the buffer."""
        self._position_counter += 1
        
        song = BufferedSong(
            identifier=recommendation.identifier,
            recommendation=recommendation,
            added_at=time.time(),
            position=self._position_counter,
            slot_type=slot_type
        )
        
        self._buffer.append(song)
        
        # Track in buffered_ids (not played_ids - only mark played when actually popped)
        if song.song_id:
            self._buffered_ids.add(song.song_id)
        
        return song
    
    def pop(self) -> Optional[BufferedSong]:
        """Get and remove the next song from the buffer."""
        if not self._buffer:
            return None
        song = self._buffer.popleft()
        
        if song.song_id:
            self._played_ids.add(song.song_id)
        
        return song
    
    def peek(self) -> Optional[BufferedSong]:
        """Peek at the next song without removing it."""
        if not self._buffer:
            return None
        return self._buffer[0]
    
    def peek_all(self) -> list[BufferedSong]:
        """Get all songs in buffer without removing."""
        return list(self._buffer)
    
    def clear(self) -> list[BufferedSong]:
        """Clear the buffer and return cleared songs."""
        cleared = list(self._buffer)
        self._buffer.clear()
        return cleared
    
    def remove_by_id(self, song_id: str) -> Optional[BufferedSong]:
        """Remove a specific song from the buffer."""
        for i, song in enumerate(self._buffer):
            if song.song_id == song_id:
                del self._buffer[i]
                return song
        return None
    
    @property
    def current_size(self) -> int:
        return len(self._buffer)
    
    @property
    def needs_refill(self) -> bool:
        return len(self._buffer) < self.size
    
    @property
    def slots_needed(self) -> int:
        return max(0, self.size - len(self._buffer))
    
    def get_exclude_set(self) -> set[str]:
        """Get set of song IDs to exclude from recommendations.
        
        Includes:
        - Songs already played this session
        - Songs currently in the buffer
        - Songs that were recently buffered (even if cleared)
        """
        exclude = set(self._played_ids)
        exclude.update(self._buffered_ids)
        for song in self._buffer:
            if song.song_id:
                exclude.add(song.song_id)
        return exclude
    
    def get_seed_songs(self, count: int = 5) -> list[SongIdentifier]:
        """Get recent songs to use as recommendation seeds."""
        # Include current buffer items
        seeds = [song.identifier for song in list(self._buffer)[:count]]
        return seeds
    
    def get_slot_type_counts(self) -> dict[BufferSlotType, int]:
        """Get count of each slot type currently in buffer."""
        counts = {BufferSlotType.SAFE: 0, BufferSlotType.EXPLORATORY: 0}
        for song in self._buffer:
            counts[song.slot_type] = counts.get(song.slot_type, 0) + 1
        return counts
    
    def get_slots_needed_by_type(
        self,
        session_state: SessionState
    ) -> dict[BufferSlotType, int]:
        """
        Calculate how many of each slot type are needed.
        
        Based on Apple Music-style buffer composition that varies by session state.
        """
        target_safe, target_exploratory = BUFFER_COMPOSITION.get(
            session_state,
            BUFFER_COMPOSITION[SessionState.WARM]
        )
        
        current_counts = self.get_slot_type_counts()
        
        # Calculate total slots needed
        total_needed = self.size - len(self._buffer)
        if total_needed <= 0:
            return {BufferSlotType.SAFE: 0, BufferSlotType.EXPLORATORY: 0}
        
        # Calculate ideal distribution for remaining slots
        current_safe = current_counts[BufferSlotType.SAFE]
        current_exploratory = current_counts[BufferSlotType.EXPLORATORY]
        
        # How many of each type are we missing from target?
        safe_deficit = max(0, target_safe - current_safe)
        exploratory_deficit = max(0, target_exploratory - current_exploratory)
        
        # Distribute needed slots proportionally
        total_deficit = safe_deficit + exploratory_deficit
        if total_deficit == 0:
            # Already at target ratios, split evenly with bias to safe
            return {
                BufferSlotType.SAFE: (total_needed + 1) // 2,
                BufferSlotType.EXPLORATORY: total_needed // 2
            }
        
        # Proportional allocation
        safe_allocation = min(total_needed, round(total_needed * safe_deficit / total_deficit))
        exploratory_allocation = total_needed - safe_allocation
        
        return {
            BufferSlotType.SAFE: safe_allocation,
            BufferSlotType.EXPLORATORY: exploratory_allocation
        }
    
    def record_exploratory_outcome(self, completed: bool) -> None:
        """Record whether an exploratory song was completed or skipped."""
        self._exploratory_outcomes.append(completed)
        # Keep only last 20 outcomes
        if len(self._exploratory_outcomes) > 20:
            self._exploratory_outcomes = self._exploratory_outcomes[-20:]
    
    def get_exploratory_success_rate(self) -> float:
        """Get success rate of exploratory picks (0.0 to 1.0)."""
        if not self._exploratory_outcomes:
            return 0.5  # Default to neutral
        return sum(self._exploratory_outcomes) / len(self._exploratory_outcomes)


class BufferManager:
    """
    Manages recommendation buffers for all sessions.
    
    Responsibilities:
    - Maintain 5-song lookahead buffer per session
    - Trigger refills when buffer runs low
    - Handle context shifts (clear and regenerate buffer)
    - Queue buffer songs for priority analysis
    - Provide next song to playback system
    
    Apple Music-Style Features:
    - "Testing the Waters" buffer composition
    - SAFE slots: High-confidence picks from proven preferences
    - EXPLORATORY slots: Discovery picks testing new music
    - Buffer regeneration on 3+ consecutive skips
    - Smooth transitions via pre-analysis
    - Context-aware buffer updates
    - Tracks exploratory success to adapt discovery aggression
    """
    
    BUFFER_SIZE = 5
    MIN_REFILL_INTERVAL = 2.0  # Seconds between refill attempts
    
    def __init__(
        self,
        config: Optional[V3Config] = None,
        recommender: Optional[Recommender] = None,
        session_mgr: Optional[SessionManager] = None,
        analyzer: Optional[SongAnalyzer] = None,
        event_bus: Optional[EventBus] = None
    ):
        """
        Initialize buffer manager.
        
        Args:
            config: V3 configuration
            recommender: Recommender for getting songs
            session_mgr: Session manager
            analyzer: Song analyzer for priority queuing
            event_bus: Event bus for notifications
        """
        self.config = config or V3Config()
        self.recommender = recommender or get_recommender()
        self.session_mgr = session_mgr or get_session_manager()
        self.analyzer = analyzer or get_song_analyzer()
        self.event_bus = event_bus or EventBus()
        
        # Buffers per session
        self._buffers: dict[str, SessionBuffer] = {}
        
        # Background refill task
        self._refill_task: Optional[asyncio.Task] = None
        self._running = False
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize and start background refill."""
        if self._initialized:
            return
        
        await self.recommender.initialize()
        await self.session_mgr.initialize()
        await self.analyzer.initialize()
        
        # Subscribe to context shift events (subscribe is synchronous)
        self.event_bus.subscribe(
            EventType.CONTEXT_SHIFT,
            self._on_context_shift
        )
        
        # Start background refill loop
        self._running = True
        self._refill_task = asyncio.create_task(self._refill_loop())
        
        self._initialized = True
        logger.info("Buffer manager initialized")
    
    async def shutdown(self) -> None:
        """Stop background tasks and clean up."""
        self._running = False
        
        if self._refill_task:
            self._refill_task.cancel()
            try:
                await self._refill_task
            except asyncio.CancelledError:
                pass
        
        self.event_bus.unsubscribe(
            EventType.CONTEXT_SHIFT,
            self._on_context_shift
        )
        
        self._initialized = False
    
    def get_buffer(self, session_id: str) -> SessionBuffer:
        """Get or create buffer for a session."""
        if session_id not in self._buffers:
            self._buffers[session_id] = SessionBuffer(
                session_id,
                self.BUFFER_SIZE
            )
        return self._buffers[session_id]
    
    def remove_buffer(self, session_id: str) -> None:
        """Remove buffer for a session."""
        self._buffers.pop(session_id, None)
    
    async def get_next_song(
        self,
        session_id: str,
        guild_id: str,
        previous_completed: bool = True
    ) -> Optional[BufferedSong]:
        """
        Get the next song to play.
        
        Pops from buffer and triggers refill if needed.
        Also tracks outcomes for exploratory picks.
        
        Args:
            session_id: Session identifier
            guild_id: Discord guild ID
            previous_completed: Whether previous song was completed (True) or skipped (False)
            
        Returns:
            Next buffered song, or None if buffer empty
        """
        await self.initialize()
        
        buffer = self.get_buffer(session_id)
        
        # Record outcome of previous song if it was exploratory
        
        song = buffer.pop()
        
        if song:
            # Queue for immediate analysis if not analyzed
            await self.analyzer.enqueue(
                song.identifier,
                priority=AnalysisPriority.IMMEDIATE
            )
            
            slot_type_str = "exploratory" if song.is_exploratory else "safe"
            logger.debug(
                f"Serving {slot_type_str} song from buffer: {song.identifier.artist} - "
                f"{song.identifier.title} ({buffer.current_size} remaining)"
            )
        
        # Trigger refill if needed
        if buffer.needs_refill:
            asyncio.create_task(self._refill_buffer(session_id, guild_id))
        
        return song
    
    def record_song_outcome(
        self,
        session_id: str,
        song_id: str,
        completed: bool
    ) -> None:
        """
        Record outcome of a played song for adaptive buffer composition.
        
        Call this when a song finishes playing (completed=True) or is skipped (completed=False).
        Used to adjust exploratory pick aggressiveness.
        
        Args:
            session_id: Session identifier
            song_id: Song that was played
            completed: True if song was completed (90%+), False if skipped
        """
        buffer = self._buffers.get(session_id)
        if not buffer:
            return
        
        # We'd need to track which songs were exploratory
        # For now, record the outcome (would need metadata lookup in practice)
        # This is a simplified implementation - full version would track slot_type per song
        buffer.record_exploratory_outcome(completed)
    
    async def peek_next(self, session_id: str) -> Optional[BufferedSong]:
        """Peek at next song without removing."""
        buffer = self.get_buffer(session_id)
        return buffer.peek()
    
    async def get_buffer_contents(
        self,
        session_id: str
    ) -> list[BufferedSong]:
        """Get all songs in buffer."""
        buffer = self.get_buffer(session_id)
        return buffer.peek_all()
    
    async def fill_initial_buffer(
        self,
        session_id: str,
        guild_id: str,
        seed_songs: list[SongIdentifier]
    ) -> int:
        """
        Fill buffer with initial recommendations using Apple Music-style composition.
        
        Called when autoplay starts. Uses slot-typed approach:
        - Gets safe picks based on seed songs and preferences
        - Gets exploratory picks for discovery
        - Mixes them according to session state
        
        Args:
            session_id: Session identifier
            guild_id: Discord guild ID
            seed_songs: Initial songs to seed recommendations
            
        Returns:
            Number of songs added to buffer
        """
        await self.initialize()
        
        buffer = self.get_buffer(session_id)
        
        # Clear any existing buffer
        buffer.clear()
        
        # Add seed songs to played set (don't recommend them)
        for seed in seed_songs:
            if seed.primary_id:
                buffer._played_ids.add(seed.primary_id)
        
        # Get current session state for buffer composition
        session = await self.session_mgr.get_session(session_id=session_id)
        session_state = session.state if session else SessionState.COLD
        
        # Determine slot allocation
        slots_needed = buffer.get_slots_needed_by_type(session_state)
        safe_count = slots_needed[BufferSlotType.SAFE]
        exploratory_count = slots_needed[BufferSlotType.EXPLORATORY]
        
        added_count = 0
        
        # Get SAFE recommendations (high confidence picks)
        if safe_count > 0:
            safe_recommendations = await self.recommender.get_recommendation(
                session_id=session_id,
                guild_id=guild_id,
                seed_songs=seed_songs,
                exclude_songs=buffer.get_exclude_set(),
                count=safe_count,
                mode="safe"  # Request conservative picks
            )
            
            for rec in safe_recommendations:
                buffer.add(rec, slot_type=BufferSlotType.SAFE)
                await self.analyzer.enqueue(
                    rec.identifier,
                    priority=AnalysisPriority.HIGH
                )
                added_count += 1
        
        # Get EXPLORATORY recommendations (discovery picks)
        if exploratory_count > 0:
            exploratory_recommendations = await self.recommender.get_recommendation(
                session_id=session_id,
                guild_id=guild_id,
                seed_songs=seed_songs,
                exclude_songs=buffer.get_exclude_set(),
                count=exploratory_count,
                mode="exploratory"  # Request diverse/adventurous picks
            )
            
            for rec in exploratory_recommendations:
                buffer.add(rec, slot_type=BufferSlotType.EXPLORATORY)
                await self.analyzer.enqueue(
                    rec.identifier,
                    priority=AnalysisPriority.HIGH
                )
                added_count += 1
        
        logger.info(
            f"Filled buffer with {added_count} songs for session {session_id} "
            f"(safe={safe_count}, exploratory={exploratory_count})"
        )
        
        return added_count
    
    async def _refill_buffer(
        self,
        session_id: str,
        guild_id: str
    ) -> int:
        """
        Refill buffer to capacity with Apple Music-style slot composition.
        
        Uses session state to determine safe vs exploratory balance.
        
        Args:
            session_id: Session identifier
            guild_id: Discord guild ID
            
        Returns:
            Number of songs added
        """
        buffer = self.get_buffer(session_id)
        
        # Check refill rate limiting
        async with buffer._refill_lock:
            now = time.time()
            if now - buffer._last_refill < self.MIN_REFILL_INTERVAL:
                return 0
            buffer._last_refill = now
            
            if not buffer.needs_refill:
                return 0
            
            # Get session for state and seeds
            session = await self.session_mgr.get_session(session_id=session_id)
            session_state = session.state if session else SessionState.COLD
            
            seed_songs = buffer.get_seed_songs()
            if not seed_songs and session:
                # Try to get from current song
                if session.current_song_id:
                    # Would need to resolve this to SongIdentifier
                    pass
            
            # Determine slot allocation based on session state
            slots_needed = buffer.get_slots_needed_by_type(session_state)
            
            # Adjust exploratory allocation based on success rate
            # If exploratory picks keep getting skipped, be more conservative
            success_rate = buffer.get_exploratory_success_rate()
            if success_rate < 0.3:
                # Exploratory picks are failing, shift to safer picks
                total = slots_needed[BufferSlotType.SAFE] + slots_needed[BufferSlotType.EXPLORATORY]
                slots_needed[BufferSlotType.SAFE] = min(total, slots_needed[BufferSlotType.SAFE] + 1)
                slots_needed[BufferSlotType.EXPLORATORY] = max(0, total - slots_needed[BufferSlotType.SAFE])
            elif success_rate > 0.7:
                # Exploratory picks are succeeding, be more adventurous
                total = slots_needed[BufferSlotType.SAFE] + slots_needed[BufferSlotType.EXPLORATORY]
                slots_needed[BufferSlotType.EXPLORATORY] = min(total, slots_needed[BufferSlotType.EXPLORATORY] + 1)
                slots_needed[BufferSlotType.SAFE] = max(0, total - slots_needed[BufferSlotType.EXPLORATORY])
            
            added = 0
            exclude = buffer.get_exclude_set()
            
            # Get SAFE recommendations
            safe_count = slots_needed[BufferSlotType.SAFE]
            if safe_count > 0:
                try:
                    safe_recs = await self.recommender.get_recommendation(
                        session_id=session_id,
                        guild_id=guild_id,
                        seed_songs=seed_songs,
                        exclude_songs=exclude,
                        count=safe_count,
                        mode="safe"
                    )
                    
                    for rec in safe_recs:
                        buffer.add(rec, slot_type=BufferSlotType.SAFE)
                        exclude.add(rec.identifier.primary_id)
                        added += 1
                        await self.analyzer.enqueue(
                            rec.identifier,
                            priority=AnalysisPriority.HIGH
                        )
                except Exception as e:
                    logger.error(f"Safe refill failed: {e}")
            
            # Get EXPLORATORY recommendations
            exploratory_count = slots_needed[BufferSlotType.EXPLORATORY]
            if exploratory_count > 0:
                try:
                    exploratory_recs = await self.recommender.get_recommendation(
                        session_id=session_id,
                        guild_id=guild_id,
                        seed_songs=seed_songs,
                        exclude_songs=exclude,
                        count=exploratory_count,
                        mode="exploratory"
                    )
                    
                    for rec in exploratory_recs:
                        buffer.add(rec, slot_type=BufferSlotType.EXPLORATORY)
                        added += 1
                        await self.analyzer.enqueue(
                            rec.identifier,
                            priority=AnalysisPriority.HIGH
                        )
                except Exception as e:
                    logger.error(f"Exploratory refill failed: {e}")
            
            if added > 0:
                logger.debug(
                    f"Refilled buffer with {added} songs "
                    f"({buffer.current_size}/{buffer.size}, "
                    f"state={session_state.value}, success_rate={success_rate:.1%})"
                )
            
            return added
    
    async def _refill_loop(self) -> None:
        """Background loop to keep buffers filled.
        
        Uses exponential backoff on repeated failures to prevent
        spinning under error conditions (rate limits, network issues).
        """
        consecutive_errors = 0
        max_backoff = 60  # Maximum backoff in seconds
        base_interval = 5  # Normal check interval
        
        while self._running:
            try:
                # Check all active sessions
                sessions = await self.session_mgr.get_active_sessions()
                
                for session in sessions:
                    buffer = self.get_buffer(session.session_id)
                    
                    if buffer.needs_refill:
                        await self._refill_buffer(
                            session.session_id,
                            session.guild_id
                        )
                
                # Reset backoff on success
                consecutive_errors = 0
                await asyncio.sleep(base_interval)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_errors += 1
                # Exponential backoff: 10s, 20s, 40s, up to max_backoff
                backoff = min(max_backoff, 10 * (2 ** (consecutive_errors - 1)))
                logger.error(f"Refill loop error (attempt {consecutive_errors}): {e}")
                logger.debug(f"Backing off for {backoff}s")
                await asyncio.sleep(backoff)
    
    async def _on_context_shift(self, payload: EventPayload) -> None:
        """
        Handle context shift event.
        
        Regenerates buffer when context changes significantly
        (e.g., multiple skips indicating wrong direction).
        """
        session_id = payload.data.get("session_id")
        if not session_id:
            return
        
        buffer = self._buffers.get(session_id)
        if not buffer:
            return
        
        reason = payload.data.get("reason", "unknown")
        logger.info(f"Context shift for session {session_id}: {reason}")
        
        # Clear current buffer
        cleared = buffer.clear()
        
        # Get session for guild ID
        session = await self.session_mgr.get_session(session_id=session_id)
        if not session:
            return
        
        # Refill with fresh recommendations
        await self._refill_buffer(session_id, session.guild_id)
        
        # Publish event
        await self.event_bus.publish(EventPayload(
            event_type=EventType.BUFFER_CLEARED,
            data={
                "session_id": session_id,
                "cleared_count": len(cleared),
                "reason": reason
            }
        ))
    
    async def skip_current(
        self,
        session_id: str,
        guild_id: str
    ) -> Optional[BufferedSong]:
        """
        Handle skip by getting next song and triggering refill.
        
        Args:
            session_id: Session identifier
            guild_id: Discord guild ID
            
        Returns:
            Next song to play
        """
        return await self.get_next_song(session_id, guild_id)
    
    async def remove_from_buffer(
        self,
        session_id: str,
        song_id: str
    ) -> bool:
        """
        Remove a specific song from the buffer.
        
        Used when user manually removes a song from queue.
        
        Args:
            session_id: Session identifier
            song_id: Song to remove
            
        Returns:
            True if song was removed
        """
        buffer = self._buffers.get(session_id)
        if not buffer:
            return False
        
        removed = buffer.remove_by_id(song_id)
        return removed is not None
    
    def get_stats(self) -> dict[str, Any]:
        """Get buffer statistics including slot type breakdown."""
        buffer_stats = {}
        for sid, b in self._buffers.items():
            slot_counts = b.get_slot_type_counts()
            buffer_stats[sid] = {
                "size": b.current_size,
                "max_size": b.size,
                "played_count": len(b._played_ids),
                "safe_slots": slot_counts[BufferSlotType.SAFE],
                "exploratory_slots": slot_counts[BufferSlotType.EXPLORATORY],
                "exploratory_success_rate": b.get_exploratory_success_rate()
            }
        
        return {
            "active_buffers": len(self._buffers),
            "total_buffered": sum(b.current_size for b in self._buffers.values()),
            "buffers": buffer_stats
        }


# Singleton instance
_buffer_manager: Optional[BufferManager] = None


def get_buffer_manager() -> BufferManager:
    """Get global buffer manager instance."""
    global _buffer_manager
    if _buffer_manager is None:
        _buffer_manager = BufferManager()
    return _buffer_manager
