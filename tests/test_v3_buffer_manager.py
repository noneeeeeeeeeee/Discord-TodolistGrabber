"""
Tests for V3 Autoplay Engine Buffer Manager Module

Tests for 5-song Apple Music-style buffer with prefetching.
Tests both the SessionBuffer class and the BufferManager class.
"""

import pytest
import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.buffer_manager import (
    BufferManager, 
    SessionBuffer,
    BufferedSong,
    BufferSlotType,
    BUFFER_COMPOSITION
)
from modules.music.Autoplay_Engine.v3.constants import (
    V3Config,
    EventType,
    SessionState
)
from modules.music.Autoplay_Engine.v3.mappings import SongIdentifier
from modules.music.Autoplay_Engine.v3.recommender import Recommendation


def create_mock_recommendation(song_id: str, title: str, artist: str) -> Recommendation:
    """Create a mock recommendation for testing."""
    identifier = SongIdentifier(
        title=title,
        artist=artist,
        deezer_id=song_id
    )
    return Recommendation(
        identifier=identifier,
        score=0.8,
        source="test",
        reason="Test recommendation"
    )


def create_mocked_buffer_manager():
    """Create a BufferManager with all dependencies mocked."""
    config = V3Config()
    
    # Create mocked dependencies
    mock_recommender = MagicMock()
    mock_recommender.initialize = AsyncMock()
    
    mock_session_mgr = MagicMock()
    mock_session_mgr.initialize = AsyncMock()
    
    mock_analyzer = MagicMock()
    mock_analyzer.initialize = AsyncMock()
    mock_analyzer.enqueue = AsyncMock()
    
    mock_event_bus = MagicMock()
    mock_event_bus.subscribe = MagicMock()
    mock_event_bus.unsubscribe = MagicMock()
    
    manager = BufferManager(
        config=config,
        recommender=mock_recommender,
        session_mgr=mock_session_mgr,
        analyzer=mock_analyzer,
        event_bus=mock_event_bus
    )
    return manager


# ============================================================================
# SessionBuffer Tests (Low-level buffer operations)
# ============================================================================

class TestSessionBufferCreation:
    """Tests for SessionBuffer initialization."""
    
    def test_session_buffer_creation(self):
        """Verify SessionBuffer can be created."""
        buffer = SessionBuffer("session_123", size=5)
        assert buffer is not None
        assert buffer.session_id == "session_123"
        assert buffer.size == 5
    
    def test_initial_state(self):
        """New buffer should be empty."""
        buffer = SessionBuffer("session_123", size=5)
        assert buffer.current_size == 0
        assert buffer.needs_refill is True


class TestSessionBufferOperations:
    """Tests for SessionBuffer add/pop/peek operations."""
    
    @pytest.fixture
    def session_buffer(self):
        """Create a session buffer."""
        return SessionBuffer("session_123", size=5)
    
    def test_add_song(self, session_buffer):
        """Should be able to add a recommendation to buffer."""
        rec = create_mock_recommendation("1", "Test Song", "Test Artist")
        song = session_buffer.add(rec, BufferSlotType.SAFE)
        
        assert session_buffer.current_size == 1
        assert song.identifier.title == "Test Song"
        assert song.slot_type == BufferSlotType.SAFE
    
    def test_pop_returns_fifo(self, session_buffer):
        """Pop should return songs in FIFO order."""
        for i in range(3):
            rec = create_mock_recommendation(str(i), f"Song {i}", "Artist")
            session_buffer.add(rec, BufferSlotType.SAFE)
        
        song1 = session_buffer.pop()
        song2 = session_buffer.pop()
        song3 = session_buffer.pop()
        
        assert song1.identifier.title == "Song 0"
        assert song2.identifier.title == "Song 1"
        assert song3.identifier.title == "Song 2"
    
    def test_pop_empty_returns_none(self, session_buffer):
        """Pop on empty buffer should return None."""
        result = session_buffer.pop()
        assert result is None
    
    def test_peek_without_removing(self, session_buffer):
        """Peek should see next song without removing."""
        rec = create_mock_recommendation("1", "Test Song", "Artist")
        session_buffer.add(rec, BufferSlotType.SAFE)
        
        peeked = session_buffer.peek()
        assert peeked.identifier.title == "Test Song"
        assert session_buffer.current_size == 1  # Still there
    
    def test_peek_all(self, session_buffer):
        """Peek all should return all songs."""
        for i in range(3):
            rec = create_mock_recommendation(str(i), f"Song {i}", "Artist")
            session_buffer.add(rec, BufferSlotType.SAFE)
        
        all_songs = session_buffer.peek_all()
        assert len(all_songs) == 3
        assert session_buffer.current_size == 3  # All still there
    
    def test_clear(self, session_buffer):
        """Clear should empty the buffer and return cleared songs."""
        for i in range(3):
            rec = create_mock_recommendation(str(i), f"Song {i}", "Artist")
            session_buffer.add(rec, BufferSlotType.SAFE)
        
        cleared = session_buffer.clear()
        assert len(cleared) == 3
        assert session_buffer.current_size == 0


class TestSessionBufferSlotTypes:
    """Tests for buffer slot type tracking."""
    
    @pytest.fixture
    def session_buffer(self):
        return SessionBuffer("session_123", size=5)
    
    def test_add_with_different_slot_types(self, session_buffer):
        """Should track songs by slot type."""
        safe_rec = create_mock_recommendation("1", "Safe Song", "Artist")
        exp_rec = create_mock_recommendation("2", "Exploratory", "Artist")
        
        session_buffer.add(safe_rec, BufferSlotType.SAFE)
        session_buffer.add(exp_rec, BufferSlotType.EXPLORATORY)
        
        counts = session_buffer.get_slot_type_counts()
        assert counts[BufferSlotType.SAFE] == 1
        assert counts[BufferSlotType.EXPLORATORY] == 1
    
    def test_is_exploratory_property(self, session_buffer):
        """BufferedSong should expose is_exploratory property."""
        rec = create_mock_recommendation("1", "Test", "Artist")
        song = session_buffer.add(rec, BufferSlotType.EXPLORATORY)
        
        assert song.is_exploratory is True


class TestSessionBufferNeedsRefill:
    """Tests for refill detection."""
    
    def test_needs_refill_when_empty(self):
        """Empty buffer needs refill."""
        buffer = SessionBuffer("session_123", size=5)
        assert buffer.needs_refill is True
    
    def test_needs_refill_partial(self):
        """Partially filled buffer needs refill."""
        buffer = SessionBuffer("session_123", size=5)
        rec = create_mock_recommendation("1", "Song", "Artist")
        buffer.add(rec, BufferSlotType.SAFE)
        
        assert buffer.needs_refill is True
        assert buffer.slots_needed == 4
    
    def test_full_buffer_no_refill(self):
        """Full buffer should not need refill."""
        buffer = SessionBuffer("session_123", size=5)
        for i in range(5):
            rec = create_mock_recommendation(str(i), f"Song {i}", "Artist")
            buffer.add(rec, BufferSlotType.SAFE)
        
        assert buffer.needs_refill is False
        assert buffer.slots_needed == 0


class TestSessionBufferExcludeSet:
    """Tests for song exclusion tracking."""
    
    def test_exclude_set_tracks_buffered_and_played(self):
        """Exclude set should include buffered and played song IDs."""
        buffer = SessionBuffer("session_123", size=5)
        
        rec1 = create_mock_recommendation("song_1", "Song 1", "Artist")
        rec2 = create_mock_recommendation("song_2", "Song 2", "Artist")
        
        buffer.add(rec1, BufferSlotType.SAFE)
        buffer.add(rec2, BufferSlotType.SAFE)
        
        # Pop one (becomes played)
        buffer.pop()
        
        exclude = buffer.get_exclude_set()
        assert "song_1" in exclude  # Was played
        assert "song_2" in exclude  # Still buffered


class TestExploratoryOutcomeTracking:
    """Tests for tracking exploratory song success/failure."""
    
    def test_record_exploratory_outcome(self):
        """Should record exploratory song outcomes."""
        buffer = SessionBuffer("session_123", size=5)
        
        buffer.record_exploratory_outcome(completed=True)
        buffer.record_exploratory_outcome(completed=False)
        buffer.record_exploratory_outcome(completed=True)
        
        rate = buffer.get_exploratory_success_rate()
        # 2 completed out of 3 = ~0.67
        assert 0.6 <= rate <= 0.7


# ============================================================================
# BufferManager Tests (High-level manager with dependencies)
# ============================================================================

class TestBufferManagerInitialization:
    """Tests for BufferManager initialization."""
    
    def test_buffer_manager_creation(self):
        """Verify buffer manager can be created with mocked deps."""
        manager = create_mocked_buffer_manager()
        assert manager is not None
    
    def test_buffer_size_constant(self):
        """Buffer size should be defined as constant."""
        manager = create_mocked_buffer_manager()
        assert manager.BUFFER_SIZE == 5
    
    @pytest.mark.asyncio
    async def test_initialize(self):
        """Buffer manager should initialize successfully."""
        manager = create_mocked_buffer_manager()
        await manager.initialize()
        assert manager._initialized is True
        
        # Verify dependencies were initialized
        manager.recommender.initialize.assert_called_once()
        manager.session_mgr.initialize.assert_called_once()
        manager.analyzer.initialize.assert_called_once()


class TestBufferManagerGetBuffer:
    """Tests for get_buffer method."""
    
    def test_get_buffer_creates_new(self):
        """get_buffer should create buffer for new session."""
        manager = create_mocked_buffer_manager()
        buffer = manager.get_buffer("session_123")
        
        assert buffer is not None
        assert isinstance(buffer, SessionBuffer)
        assert buffer.session_id == "session_123"
    
    def test_get_buffer_returns_existing(self):
        """get_buffer should return same buffer for same session."""
        manager = create_mocked_buffer_manager()
        buffer1 = manager.get_buffer("session_123")
        buffer2 = manager.get_buffer("session_123")
        
        assert buffer1 is buffer2
    
    def test_separate_buffers_per_session(self):
        """Each session should have its own buffer."""
        manager = create_mocked_buffer_manager()
        buffer1 = manager.get_buffer("session_1")
        buffer2 = manager.get_buffer("session_2")
        
        assert buffer1 is not buffer2
        assert buffer1.session_id == "session_1"
        assert buffer2.session_id == "session_2"


class TestBufferManagerRemoveBuffer:
    """Tests for remove_buffer method."""
    
    def test_remove_buffer(self):
        """remove_buffer should delete session's buffer."""
        manager = create_mocked_buffer_manager()
        
        # Create buffer
        buffer = manager.get_buffer("session_123")
        rec = create_mock_recommendation("1", "Song", "Artist")
        buffer.add(rec, BufferSlotType.SAFE)
        
        # Remove
        manager.remove_buffer("session_123")
        
        # Get again should create new empty buffer
        new_buffer = manager.get_buffer("session_123")
        assert new_buffer.current_size == 0


class TestBufferManagerGetNextSong:
    """Tests for get_next_song method."""
    
    @pytest.mark.asyncio
    async def test_get_next_song_from_buffer(self):
        """get_next_song should pop from buffer."""
        manager = create_mocked_buffer_manager()
        await manager.initialize()
        
        # Pre-fill buffer
        buffer = manager.get_buffer("session_123")
        rec = create_mock_recommendation("1", "Test Song", "Artist")
        buffer.add(rec, BufferSlotType.SAFE)
        
        song = await manager.get_next_song("session_123", "guild_123")
        
        assert song is not None
        assert song.identifier.title == "Test Song"
        assert buffer.current_size == 0  # Was removed
    
    @pytest.mark.asyncio
    async def test_get_next_song_empty_buffer(self):
        """get_next_song on empty buffer returns None."""
        manager = create_mocked_buffer_manager()
        await manager.initialize()
        
        song = await manager.get_next_song("session_123", "guild_123")
        assert song is None
    
    @pytest.mark.asyncio
    async def test_get_next_song_queues_for_analysis(self):
        """get_next_song should queue returned song for analysis."""
        manager = create_mocked_buffer_manager()
        await manager.initialize()
        
        buffer = manager.get_buffer("session_123")
        rec = create_mock_recommendation("1", "Test Song", "Artist")
        buffer.add(rec, BufferSlotType.SAFE)
        
        await manager.get_next_song("session_123", "guild_123")
        
        # Verify analyzer.enqueue was called
        manager.analyzer.enqueue.assert_called()


# ============================================================================
# Buffer Composition Tests (Apple Music-style slot allocation)
# ============================================================================

class TestBufferComposition:
    """Tests for BUFFER_COMPOSITION configuration."""
    
    def test_slot_type_enum_exists(self):
        """BufferSlotType enum should have SAFE and EXPLORATORY."""
        assert BufferSlotType.SAFE.value == "safe"
        assert BufferSlotType.EXPLORATORY.value == "exploratory"
    
    def test_composition_by_session_state(self):
        """BUFFER_COMPOSITION should define ratios by state."""
        assert SessionState.COLD in BUFFER_COMPOSITION
        assert SessionState.WARM in BUFFER_COMPOSITION
        assert SessionState.HOT in BUFFER_COMPOSITION
        assert SessionState.EXTENDED in BUFFER_COMPOSITION
    
    def test_cold_state_composition(self):
        """Cold state should be mostly safe (4 safe, 1 exploratory)."""
        safe, exploratory = BUFFER_COMPOSITION[SessionState.COLD]
        assert safe == 4
        assert exploratory == 1
    
    def test_warm_state_composition(self):
        """Warm state should be balanced (3 safe, 2 exploratory)."""
        safe, exploratory = BUFFER_COMPOSITION[SessionState.WARM]
        assert safe == 3
        assert exploratory == 2
    
    def test_hot_state_composition(self):
        """Hot state should favor exploration (2 safe, 3 exploratory)."""
        safe, exploratory = BUFFER_COMPOSITION[SessionState.HOT]
        assert safe == 2
        assert exploratory == 3
    
    def test_extended_state_composition(self):
        """Extended state should match hot state."""
        hot_comp = BUFFER_COMPOSITION[SessionState.HOT]
        extended_comp = BUFFER_COMPOSITION[SessionState.EXTENDED]
        assert hot_comp == extended_comp


class TestSessionBufferSlotAllocation:
    """Tests for getting slots needed by type."""
    
    def test_slots_needed_by_type_empty_buffer(self):
        """Empty buffer should need full allocation based on state."""
        buffer = SessionBuffer("session_123", size=5)
        
        # For cold state (4 safe, 1 exploratory)
        needed = buffer.get_slots_needed_by_type(SessionState.COLD)
        
        assert needed[BufferSlotType.SAFE] == 4
        assert needed[BufferSlotType.EXPLORATORY] == 1
    
    def test_slots_needed_partial_fill(self):
        """Partially filled buffer should calculate remaining slots."""
        buffer = SessionBuffer("session_123", size=5)
        
        # Add 2 safe songs
        for i in range(2):
            rec = create_mock_recommendation(str(i), f"Song {i}", "Artist")
            buffer.add(rec, BufferSlotType.SAFE)
        
        # For cold state (4 safe, 1 exploratory), with 2 safe already
        needed = buffer.get_slots_needed_by_type(SessionState.COLD)
        
        # Should need 2 more safe (4-2) and 1 exploratory
        assert needed[BufferSlotType.SAFE] == 2
        assert needed[BufferSlotType.EXPLORATORY] == 1


# ============================================================================
# BufferManager Shutdown Tests
# ============================================================================

class TestBufferManagerShutdown:
    """Tests for BufferManager shutdown."""
    
    @pytest.mark.asyncio
    async def test_shutdown(self):
        """Shutdown should cancel background tasks."""
        manager = create_mocked_buffer_manager()
        await manager.initialize()
        
        # Shutdown
        await manager.shutdown()
        
        # Verify running flag is False
        assert manager._running is False

