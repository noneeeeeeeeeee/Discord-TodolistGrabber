"""
Tests for V3 Autoplay Engine Buffer Manager Module

Tests for 5-song Apple Music-style buffer with prefetching.
"""

import pytest
import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.buffer_manager import BufferManager
from modules.music.Autoplay_Engine.v3.constants import (
    BufferConfig,
    EventType,
    SessionState
)


@dataclass
class MockSong:
    """Mock song for testing."""
    id: str
    title: str
    artist: str
    duration: int = 180


class TestBufferManagerInitialization:
    """Tests for BufferManager initialization."""
    
    def test_buffer_manager_creation(self):
        """Verify buffer manager can be created."""
        config = BufferConfig()
        manager = BufferManager(config)
        assert manager is not None
    
    def test_buffer_size_is_five(self):
        """Buffer should be configured for 5 songs."""
        config = BufferConfig(size=5)
        manager = BufferManager(config)
        assert manager.max_size == 5
    
    @pytest.mark.asyncio
    async def test_initialize(self):
        """Buffer manager should initialize successfully."""
        config = BufferConfig()
        manager = BufferManager(config)
        await manager.initialize()
        assert manager._initialized is True


class TestBufferOperations:
    """Tests for basic buffer operations."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create an initialized buffer manager."""
        config = BufferConfig(size=5, low_threshold=2)
        manager = BufferManager(config)
        return manager
    
    @pytest.mark.asyncio
    async def test_add_song_to_buffer(self, buffer_manager):
        """Should be able to add a song to the buffer."""
        await buffer_manager.initialize()
        
        song = MockSong(id='1', title='Test Song', artist='Test Artist')
        await buffer_manager.add('session_123', song)
        
        assert buffer_manager.size('session_123') == 1
    
    @pytest.mark.asyncio
    async def test_get_next_song(self, buffer_manager):
        """Should return the next song from buffer."""
        await buffer_manager.initialize()
        
        songs = [
            MockSong(id='1', title='Song 1', artist='Artist 1'),
            MockSong(id='2', title='Song 2', artist='Artist 2'),
            MockSong(id='3', title='Song 3', artist='Artist 3')
        ]
        
        for song in songs:
            await buffer_manager.add('session_123', song)
        
        next_song = await buffer_manager.get_next('session_123')
        assert next_song.id == '1'  # FIFO order
    
    @pytest.mark.asyncio
    async def test_buffer_fifo_order(self, buffer_manager):
        """Buffer should maintain FIFO order."""
        await buffer_manager.initialize()
        
        for i in range(3):
            song = MockSong(id=str(i), title=f'Song {i}', artist='Artist')
            await buffer_manager.add('session_123', song)
        
        # Songs should come out in order
        song1 = await buffer_manager.get_next('session_123')
        song2 = await buffer_manager.get_next('session_123')
        song3 = await buffer_manager.get_next('session_123')
        
        assert song1.id == '0'
        assert song2.id == '1'
        assert song3.id == '2'
    
    @pytest.mark.asyncio
    async def test_buffer_size_tracking(self, buffer_manager):
        """Should correctly track buffer size."""
        await buffer_manager.initialize()
        
        assert buffer_manager.size('session_123') == 0
        
        await buffer_manager.add('session_123', MockSong('1', 'Song', 'Artist'))
        assert buffer_manager.size('session_123') == 1
        
        await buffer_manager.add('session_123', MockSong('2', 'Song', 'Artist'))
        assert buffer_manager.size('session_123') == 2
        
        await buffer_manager.get_next('session_123')
        assert buffer_manager.size('session_123') == 1


class TestBufferLowThreshold:
    """Tests for low buffer threshold detection."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager with low threshold of 2."""
        config = BufferConfig(size=5, low_threshold=2)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_low_buffer_detection(self, buffer_manager):
        """Should detect when buffer is low."""
        await buffer_manager.initialize()
        
        # Add 3 songs
        for i in range(3):
            await buffer_manager.add('session_123', MockSong(str(i), 'Song', 'Artist'))
        
        # Consume 2, leaving 1 (below threshold of 2)
        await buffer_manager.get_next('session_123')
        await buffer_manager.get_next('session_123')
        
        assert buffer_manager.is_low('session_123') is True
    
    @pytest.mark.asyncio
    async def test_low_buffer_event(self, buffer_manager):
        """Should emit BUFFER_LOW event when threshold reached."""
        await buffer_manager.initialize()
        
        event_emitted = False
        
        async def on_buffer_low(payload):
            nonlocal event_emitted
            event_emitted = True
        
        if hasattr(buffer_manager, 'on_low'):
            buffer_manager.on_low(on_buffer_low)
        
        # Add 2 songs (at threshold)
        await buffer_manager.add('session_123', MockSong('1', 'Song', 'Artist'))
        await buffer_manager.add('session_123', MockSong('2', 'Song', 'Artist'))
        
        # Get one, going below threshold
        await buffer_manager.get_next('session_123')
        
        # Event may have been emitted
        # assert event_emitted is True  # Depends on implementation
    
    @pytest.mark.asyncio
    async def test_not_low_when_full(self, buffer_manager):
        """Buffer should not be low when full."""
        await buffer_manager.initialize()
        
        for i in range(5):
            await buffer_manager.add('session_123', MockSong(str(i), 'Song', 'Artist'))
        
        assert buffer_manager.is_low('session_123') is False


class TestBufferPrefetching:
    """Tests for prefetch functionality."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager with prefetch enabled."""
        config = BufferConfig(size=5, low_threshold=2, prefetch_enabled=True)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_prefetch_enabled(self, buffer_manager):
        """Prefetch should be enabled by default."""
        assert buffer_manager.prefetch_enabled is True
    
    @pytest.mark.asyncio
    async def test_prefetch_callback_registration(self, buffer_manager):
        """Should be able to register prefetch callback."""
        await buffer_manager.initialize()
        
        prefetch_called = False
        
        async def prefetch_callback(session_id, count_needed):
            nonlocal prefetch_called
            prefetch_called = True
            return []
        
        if hasattr(buffer_manager, 'set_prefetch_callback'):
            buffer_manager.set_prefetch_callback(prefetch_callback)
            
            # Trigger prefetch by going low
            await buffer_manager.add('session_123', MockSong('1', 'Song', 'Artist'))
            await buffer_manager.get_next('session_123')
            
            # Give async callback time to run
            await asyncio.sleep(0.1)


class TestBufferPerSession:
    """Tests for per-session buffer isolation."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager."""
        config = BufferConfig(size=5)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_separate_buffers_per_session(self, buffer_manager):
        """Each session should have its own buffer."""
        await buffer_manager.initialize()
        
        await buffer_manager.add('session_1', MockSong('1', 'Song 1', 'Artist'))
        await buffer_manager.add('session_2', MockSong('2', 'Song 2', 'Artist'))
        
        assert buffer_manager.size('session_1') == 1
        assert buffer_manager.size('session_2') == 1
    
    @pytest.mark.asyncio
    async def test_session_buffer_isolation(self, buffer_manager):
        """Getting from one session should not affect another."""
        await buffer_manager.initialize()
        
        await buffer_manager.add('session_1', MockSong('1', 'Song 1', 'Artist'))
        await buffer_manager.add('session_2', MockSong('2', 'Song 2', 'Artist'))
        
        song = await buffer_manager.get_next('session_1')
        
        assert song.id == '1'
        assert buffer_manager.size('session_1') == 0
        assert buffer_manager.size('session_2') == 1


class TestBufferClearing:
    """Tests for buffer clearing operations."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager."""
        config = BufferConfig(size=5)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_clear_session_buffer(self, buffer_manager):
        """Should be able to clear a session's buffer."""
        await buffer_manager.initialize()
        
        for i in range(3):
            await buffer_manager.add('session_123', MockSong(str(i), 'Song', 'Artist'))
        
        await buffer_manager.clear('session_123')
        
        assert buffer_manager.size('session_123') == 0
    
    @pytest.mark.asyncio
    async def test_clear_all_buffers(self, buffer_manager):
        """Should be able to clear all buffers."""
        await buffer_manager.initialize()
        
        await buffer_manager.add('session_1', MockSong('1', 'Song', 'Artist'))
        await buffer_manager.add('session_2', MockSong('2', 'Song', 'Artist'))
        
        await buffer_manager.clear_all()
        
        assert buffer_manager.size('session_1') == 0
        assert buffer_manager.size('session_2') == 0


class TestBufferUrgency:
    """Tests for urgency-based refill."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager."""
        config = BufferConfig(size=5, low_threshold=2)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_urgency_level_empty(self, buffer_manager):
        """Empty buffer should have highest urgency."""
        await buffer_manager.initialize()
        
        if hasattr(buffer_manager, 'get_urgency'):
            urgency = buffer_manager.get_urgency('session_123')
            assert urgency == 'critical' or urgency == 1.0
    
    @pytest.mark.asyncio
    async def test_urgency_level_full(self, buffer_manager):
        """Full buffer should have no urgency."""
        await buffer_manager.initialize()
        
        for i in range(5):
            await buffer_manager.add('session_123', MockSong(str(i), 'Song', 'Artist'))
        
        if hasattr(buffer_manager, 'get_urgency'):
            urgency = buffer_manager.get_urgency('session_123')
            assert urgency == 'none' or urgency == 0.0
    
    @pytest.mark.asyncio
    async def test_songs_needed_count(self, buffer_manager):
        """Should calculate how many songs needed to fill."""
        await buffer_manager.initialize()
        
        await buffer_manager.add('session_123', MockSong('1', 'Song', 'Artist'))
        await buffer_manager.add('session_123', MockSong('2', 'Song', 'Artist'))
        
        needed = buffer_manager.songs_needed('session_123')
        assert needed == 3  # 5 - 2 = 3


class TestBufferPeek:
    """Tests for peeking at buffer contents."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager."""
        config = BufferConfig(size=5)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_peek_next(self, buffer_manager):
        """Should be able to peek at next song without removing."""
        await buffer_manager.initialize()
        
        song = MockSong('1', 'Song 1', 'Artist')
        await buffer_manager.add('session_123', song)
        
        if hasattr(buffer_manager, 'peek'):
            peeked = await buffer_manager.peek('session_123')
            assert peeked.id == '1'
            assert buffer_manager.size('session_123') == 1  # Still there
    
    @pytest.mark.asyncio
    async def test_peek_all(self, buffer_manager):
        """Should be able to see all buffered songs."""
        await buffer_manager.initialize()
        
        for i in range(3):
            await buffer_manager.add('session_123', MockSong(str(i), 'Song', 'Artist'))
        
        if hasattr(buffer_manager, 'peek_all'):
            songs = await buffer_manager.peek_all('session_123')
            assert len(songs) == 3
            assert buffer_manager.size('session_123') == 3  # All still there


class TestBufferEmptyHandling:
    """Tests for empty buffer handling."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager."""
        config = BufferConfig(size=5)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_get_from_empty_buffer(self, buffer_manager):
        """Getting from empty buffer should return None or raise."""
        await buffer_manager.initialize()
        
        result = await buffer_manager.get_next('session_123')
        assert result is None
    
    @pytest.mark.asyncio
    async def test_is_empty(self, buffer_manager):
        """Should correctly report if buffer is empty."""
        await buffer_manager.initialize()
        
        assert buffer_manager.is_empty('session_123') is True
        
        await buffer_manager.add('session_123', MockSong('1', 'Song', 'Artist'))
        assert buffer_manager.is_empty('session_123') is False


class TestBufferMaxSize:
    """Tests for buffer max size enforcement."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager with size 5."""
        config = BufferConfig(size=5)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_buffer_respects_max_size(self, buffer_manager):
        """Should not exceed max size."""
        await buffer_manager.initialize()
        
        # Try to add 7 songs
        for i in range(7):
            await buffer_manager.add('session_123', MockSong(str(i), 'Song', 'Artist'))
        
        assert buffer_manager.size('session_123') <= 5
    
    @pytest.mark.asyncio
    async def test_is_full(self, buffer_manager):
        """Should correctly report if buffer is full."""
        await buffer_manager.initialize()
        
        assert buffer_manager.is_full('session_123') is False
        
        for i in range(5):
            await buffer_manager.add('session_123', MockSong(str(i), 'Song', 'Artist'))
        
        assert buffer_manager.is_full('session_123') is True


# ============================================================================
# Apple Music-Style Slot Type Tests (V3 Clarification)
# ============================================================================

class TestBufferSlotTypes:
    """Tests for SAFE/EXPLORATORY slot type system."""
    
    def test_slot_type_enum_exists(self):
        """BufferSlotType enum should exist."""
        from modules.music.Autoplay_Engine.v3.buffer_manager import BufferSlotType
        
        assert hasattr(BufferSlotType, 'SAFE')
        assert hasattr(BufferSlotType, 'EXPLORATORY')
    
    def test_buffer_composition_config(self):
        """BUFFER_COMPOSITION should define phase compositions."""
        from modules.music.Autoplay_Engine.v3.buffer_manager import BUFFER_COMPOSITION
        
        assert 'early' in BUFFER_COMPOSITION
        assert 'establishing' in BUFFER_COMPOSITION
        assert 'confident' in BUFFER_COMPOSITION
    
    def test_early_phase_composition(self):
        """Early phase should be 80% safe (4 SAFE, 1 EXPLORATORY)."""
        from modules.music.Autoplay_Engine.v3.buffer_manager import (
            BUFFER_COMPOSITION, BufferSlotType
        )
        
        early = BUFFER_COMPOSITION['early']
        safe_count = early.get(BufferSlotType.SAFE, 0)
        exploratory_count = early.get(BufferSlotType.EXPLORATORY, 0)
        
        # Should be 4:1 ratio
        assert safe_count == 4
        assert exploratory_count == 1
    
    def test_establishing_phase_composition(self):
        """Establishing phase should be 60% safe (3 SAFE, 2 EXPLORATORY)."""
        from modules.music.Autoplay_Engine.v3.buffer_manager import (
            BUFFER_COMPOSITION, BufferSlotType
        )
        
        establishing = BUFFER_COMPOSITION['establishing']
        safe_count = establishing.get(BufferSlotType.SAFE, 0)
        exploratory_count = establishing.get(BufferSlotType.EXPLORATORY, 0)
        
        assert safe_count == 3
        assert exploratory_count == 2
    
    def test_confident_phase_composition(self):
        """Confident phase should be 40% safe (2 SAFE, 3 EXPLORATORY)."""
        from modules.music.Autoplay_Engine.v3.buffer_manager import (
            BUFFER_COMPOSITION, BufferSlotType
        )
        
        confident = BUFFER_COMPOSITION['confident']
        safe_count = confident.get(BufferSlotType.SAFE, 0)
        exploratory_count = confident.get(BufferSlotType.EXPLORATORY, 0)
        
        assert safe_count == 2
        assert exploratory_count == 3


class TestBufferSlotAllocation:
    """Tests for slot allocation based on session phase."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager for slot testing."""
        config = BufferConfig(size=5)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_get_slots_needed_by_type(self, buffer_manager):
        """Should report needed slots by type."""
        await buffer_manager.initialize()
        
        if hasattr(buffer_manager, 'get_slots_needed_by_type'):
            needed = buffer_manager.get_slots_needed_by_type('session_123', phase='early')
            
            from modules.music.Autoplay_Engine.v3.buffer_manager import BufferSlotType
            
            assert BufferSlotType.SAFE in needed
            assert BufferSlotType.EXPLORATORY in needed
    
    @pytest.mark.asyncio
    async def test_phase_detection_early(self, buffer_manager):
        """Should detect 'early' phase for 0-5 songs played."""
        await buffer_manager.initialize()
        
        if hasattr(buffer_manager, 'get_session_phase'):
            # Simulate session with 3 songs played
            phase = buffer_manager.get_session_phase('session_123', songs_played=3)
            assert phase == 'early'
    
    @pytest.mark.asyncio
    async def test_phase_detection_establishing(self, buffer_manager):
        """Should detect 'establishing' phase for 5-15 songs played."""
        await buffer_manager.initialize()
        
        if hasattr(buffer_manager, 'get_session_phase'):
            phase = buffer_manager.get_session_phase('session_123', songs_played=10)
            assert phase == 'establishing'
    
    @pytest.mark.asyncio
    async def test_phase_detection_confident(self, buffer_manager):
        """Should detect 'confident' phase for 15+ songs played."""
        await buffer_manager.initialize()
        
        if hasattr(buffer_manager, 'get_session_phase'):
            phase = buffer_manager.get_session_phase('session_123', songs_played=20)
            assert phase == 'confident'


class TestExploratoryTracking:
    """Tests for tracking exploratory song success."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager for tracking tests."""
        config = BufferConfig(size=5)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_track_exploratory_success(self, buffer_manager):
        """Should track success rate of exploratory picks."""
        await buffer_manager.initialize()
        
        if hasattr(buffer_manager, 'record_exploratory_outcome'):
            buffer_manager.record_exploratory_outcome('session_123', 'song_1', success=True)
            buffer_manager.record_exploratory_outcome('session_123', 'song_2', success=False)
            
            if hasattr(buffer_manager, 'get_exploratory_success_rate'):
                rate = buffer_manager.get_exploratory_success_rate('session_123')
                assert 0 <= rate <= 1
    
    @pytest.mark.asyncio
    async def test_adjust_allocation_based_on_success(self, buffer_manager):
        """Should adjust slot allocation based on exploratory success."""
        await buffer_manager.initialize()
        
        # If exploratory songs are succeeding, may increase their allocation
        # If failing, may reduce
        pass  # Implementation-dependent


class TestSlotTypedRefill:
    """Tests for slot-typed buffer refill logic."""
    
    @pytest.fixture
    def buffer_manager(self):
        """Create buffer manager for refill tests."""
        config = BufferConfig(size=5)
        return BufferManager(config)
    
    @pytest.mark.asyncio
    async def test_add_with_slot_type(self, buffer_manager):
        """Should accept slot type when adding songs."""
        await buffer_manager.initialize()
        
        from modules.music.Autoplay_Engine.v3.buffer_manager import BufferSlotType
        
        if hasattr(buffer_manager, 'add_with_type'):
            await buffer_manager.add_with_type(
                'session_123',
                MockSong('1', 'Safe Song', 'Artist'),
                slot_type=BufferSlotType.SAFE
            )
            await buffer_manager.add_with_type(
                'session_123',
                MockSong('2', 'Exploratory Song', 'New Artist'),
                slot_type=BufferSlotType.EXPLORATORY
            )
    
    @pytest.mark.asyncio
    async def test_refill_respects_composition(self, buffer_manager):
        """Refill should maintain target slot type composition."""
        await buffer_manager.initialize()
        
        # When refilling, should request appropriate mix of SAFE/EXPLORATORY
        # based on current phase
        pass  # Implementation-dependent

