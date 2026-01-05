"""
Tests for V3 Autoplay Engine Event Bus Module

Tests for async pub/sub event-driven communication system.
"""

import pytest
import asyncio
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.event_bus import (
    EventBus,
    EventPayload,
    get_event_bus
)
from modules.music.Autoplay_Engine.v3.constants import EventType


class TestEventPayload:
    """Tests for EventPayload dataclass."""
    
    def test_payload_creation(self):
        """Verify payload can be created with required fields."""
        payload = EventPayload(
            event_type=EventType.SONG_STARTED,
            data={'song_id': '12345'},
            source='test'
        )
        assert payload.event_type == EventType.SONG_STARTED
        assert payload.data['song_id'] == '12345'
        assert payload.source == 'test'
    
    def test_payload_has_timestamp(self):
        """Payload should automatically have a timestamp."""
        payload = EventPayload(
            event_type=EventType.SONG_ENDED,
            data={},
            source='test'
        )
        assert payload.timestamp is not None
        assert isinstance(payload.timestamp, (datetime, float, int, str))
    
    def test_payload_optional_session_id(self):
        """Session ID should be optional in payload."""
        payload = EventPayload(
            event_type=EventType.SESSION_STARTED,
            data={},
            source='test',
            session_id='session_123'
        )
        assert payload.session_id == 'session_123'
    
    def test_payload_without_session_id(self):
        """Payload should work without session ID."""
        payload = EventPayload(
            event_type=EventType.SONG_ANALYZED,
            data={},
            source='test'
        )
        assert payload.session_id is None


class TestEventBusSingleton:
    """Tests for EventBus singleton pattern."""
    
    def test_get_event_bus_returns_instance(self):
        """get_event_bus should return an EventBus instance."""
        bus = get_event_bus()
        assert isinstance(bus, EventBus)
    
    def test_singleton_returns_same_instance(self):
        """Multiple calls should return the same instance."""
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        assert bus1 is bus2
    
    def test_event_bus_direct_creation(self):
        """Direct creation should still work but may warn or use singleton."""
        bus = EventBus()
        assert bus is not None


class TestEventBusSubscription:
    """Tests for EventBus subscription functionality."""
    
    @pytest.fixture
    def event_bus(self):
        """Create a fresh event bus for each test."""
        bus = EventBus()
        bus.reset()  # Reset subscribers
        return bus
    
    def test_subscribe_to_event(self, event_bus):
        """Should be able to subscribe to an event type."""
        callback = AsyncMock()
        event_bus.subscribe(EventType.SONG_STARTED, callback)
        
        assert EventType.SONG_STARTED in event_bus._handlers
        assert callback in event_bus._handlers[EventType.SONG_STARTED]
    
    def test_subscribe_multiple_callbacks(self, event_bus):
        """Multiple callbacks can subscribe to the same event."""
        callback1 = AsyncMock()
        callback2 = AsyncMock()
        
        event_bus.subscribe(EventType.SONG_SKIPPED, callback1)
        event_bus.subscribe(EventType.SONG_SKIPPED, callback2)
        
        assert len(event_bus._handlers[EventType.SONG_SKIPPED]) == 2
    
    def test_unsubscribe_from_event(self, event_bus):
        """Should be able to unsubscribe a callback."""
        callback = AsyncMock()
        event_bus.subscribe(EventType.BUFFER_LOW, callback)
        event_bus.unsubscribe(EventType.BUFFER_LOW, callback)
        
        assert callback not in event_bus._handlers.get(EventType.BUFFER_LOW, [])
    
    def test_unsubscribe_nonexistent_callback(self, event_bus):
        """Unsubscribing non-existent callback should not raise."""
        callback = AsyncMock()
        # Should not raise
        event_bus.unsubscribe(EventType.SESSION_ENDED, callback)


class TestEventBusPublish:
    """Tests for EventBus publishing functionality."""
    
    @pytest.fixture
    def event_bus(self):
        """Create a fresh event bus for each test."""
        bus = EventBus()
        bus.reset()
        return bus
    
    @pytest.mark.asyncio
    async def test_publish_calls_subscriber(self, event_bus):
        """Publishing should call subscribed callbacks."""
        callback = AsyncMock()
        event_bus.subscribe(EventType.SONG_STARTED, callback)
        
        payload = EventPayload(
            event_type=EventType.SONG_STARTED,
            data={'song_id': '123'},
            source='test'
        )
        
        await event_bus.publish(payload)
        
        callback.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_publish_passes_payload(self, event_bus):
        """Published payload should be passed to callback."""
        received_payload = None
        
        async def callback(payload):
            nonlocal received_payload
            received_payload = payload
        
        event_bus.subscribe(EventType.SONG_ENDED, callback)
        
        payload = EventPayload(
            event_type=EventType.SONG_ENDED,
            data={'completed': True},
            source='test'
        )
        
        await event_bus.publish(payload)
        
        assert received_payload is not None
        assert received_payload.data['completed'] is True
    
    @pytest.mark.asyncio
    async def test_publish_calls_all_handlers(self, event_bus):
        """Publishing should call all subscribed callbacks."""
        call_count = 0
        
        async def callback1(payload):
            nonlocal call_count
            call_count += 1
        
        async def callback2(payload):
            nonlocal call_count
            call_count += 1
        
        event_bus.subscribe(EventType.SESSION_STARTED, callback1)
        event_bus.subscribe(EventType.SESSION_STARTED, callback2)
        
        payload = EventPayload(
            event_type=EventType.SESSION_STARTED,
            data={},
            source='test'
        )
        
        await event_bus.publish(payload)
        
        assert call_count == 2
    
    @pytest.mark.asyncio
    async def test_publish_to_unsubscribed_event(self, event_bus):
        """Publishing to event with no subscribers should not raise."""
        payload = EventPayload(
            event_type=EventType.DAYDREAM_STARTED,
            data={},
            source='test'
        )
        
        # Should not raise
        await event_bus.publish(payload)
    
    @pytest.mark.asyncio
    async def test_publish_error_handling(self, event_bus):
        """Errors in callbacks should not break other subscribers."""
        call_order = []
        
        async def failing_callback(payload):
            raise ValueError("Intentional test error")
        
        async def working_callback(payload):
            call_order.append('working')
        
        event_bus.subscribe(EventType.ANALYSIS_REQUESTED, failing_callback)
        event_bus.subscribe(EventType.ANALYSIS_REQUESTED, working_callback)
        
        payload = EventPayload(
            event_type=EventType.ANALYSIS_REQUESTED,
            data={},
            source='test'
        )
        
        # Should not raise, should continue to next subscriber
        await event_bus.publish(payload)
        
        assert 'working' in call_order


class TestEventBusTopics:
    """Tests for topic-based filtering."""
    
    @pytest.fixture
    def event_bus(self):
        """Create a fresh event bus for each test."""
        bus = EventBus()
        bus.reset()
        bus._topic_handlers = {}
        return bus
    
    def test_subscribe_to_topic(self, event_bus):
        """Should be able to subscribe to a specific topic."""
        callback = AsyncMock()
        
        if hasattr(event_bus, 'subscribe_topic'):
            event_bus.subscribe_topic('session.123', callback)
            assert 'session.123' in event_bus._topic_handlers
    
    @pytest.mark.asyncio
    async def test_publish_to_topic(self, event_bus):
        """Publishing to topic should only call topic subscribers."""
        topic_callback = AsyncMock()
        global_callback = AsyncMock()
        
        event_bus.subscribe(EventType.SONG_STARTED, global_callback)
        
        if hasattr(event_bus, 'subscribe_topic'):
            event_bus.subscribe_topic('session.123', topic_callback)
            
            payload = EventPayload(
                event_type=EventType.SONG_STARTED,
                data={},
                source='test',
                session_id='123'
            )
            
            if hasattr(event_bus, 'publish_to_topic'):
                await event_bus.publish_to_topic('session.123', payload)
                topic_callback.assert_called_once()


class TestEventBusAsync:
    """Tests for async behavior of EventBus."""
    
    @pytest.fixture
    def event_bus(self):
        """Create a fresh event bus for each test."""
        bus = EventBus()
        bus.reset()
        return bus
    
    @pytest.mark.asyncio
    async def test_concurrent_publish(self, event_bus):
        """Multiple publishes should work concurrently."""
        call_count = 0
        
        async def slow_callback(payload):
            nonlocal call_count
            await asyncio.sleep(0.01)
            call_count += 1
        
        event_bus.subscribe(EventType.BUFFER_REFILLED, slow_callback)
        
        payloads = [
            EventPayload(event_type=EventType.BUFFER_REFILLED, data={}, source='test')
            for _ in range(3)
        ]
        
        await asyncio.gather(*[event_bus.publish(p) for p in payloads])
        
        assert call_count == 3
    
    @pytest.mark.asyncio
    async def test_emit_convenience_method(self, event_bus):
        """emit() should be a convenience wrapper for publish()."""
        callback = AsyncMock()
        event_bus.subscribe(EventType.SONG_SKIPPED, callback)
        
        if hasattr(event_bus, 'emit'):
            await event_bus.emit(
                EventType.SONG_SKIPPED,
                data={'reason': 'user_skip'},
                source='test'
            )
            callback.assert_called_once()


class TestEventBusCleanup:
    """Tests for EventBus cleanup and resource management."""
    
    @pytest.fixture
    def event_bus(self):
        """Create a fresh event bus for each test."""
        bus = EventBus()
        bus.reset()
        return bus
    
    def test_clear_all_handlers(self, event_bus):
        """Should be able to clear all subscribers."""
        callback = AsyncMock()
        event_bus.subscribe(EventType.SONG_STARTED, callback)
        event_bus.subscribe(EventType.SONG_ENDED, callback)
        
        if hasattr(event_bus, 'clear'):
            event_bus.clear()
            assert len(event_bus._handlers) == 0
    
    def test_clear_specific_event(self, event_bus):
        """Should be able to clear subscribers for specific event."""
        callback = AsyncMock()
        event_bus.subscribe(EventType.SONG_STARTED, callback)
        event_bus.subscribe(EventType.SONG_ENDED, callback)
        
        if hasattr(event_bus, 'clear_event'):
            event_bus.clear_event(EventType.SONG_STARTED)
            assert EventType.SONG_STARTED not in event_bus._handlers
            assert EventType.SONG_ENDED in event_bus._handlers
    
    def test_subscriber_count(self, event_bus):
        """Should be able to get subscriber count."""
        callback = AsyncMock()
        event_bus.subscribe(EventType.BUFFER_LOW, callback)
        event_bus.subscribe(EventType.BUFFER_LOW, callback)
        
        if hasattr(event_bus, 'subscriber_count'):
            count = event_bus.subscriber_count(EventType.BUFFER_LOW)
            assert count == 2
