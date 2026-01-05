"""Async event bus for pub/sub communication between V3 modules.

This module provides a centralized event-driven communication system that allows
modules to publish events and subscribe to them without direct dependencies.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set
from weakref import WeakSet

from .constants import EventType

LOG = logging.getLogger(__name__)


@dataclass
class Event:
    """Represents an event in the system."""
    event_type: EventType
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    priority: int = 0  # Higher = more important
    source: Optional[str] = None  # Module that emitted the event
    session_id: Optional[str] = None  # Session this event relates to
    
    def __post_init__(self):
        if self.priority is None:
            self.priority = 0


# EventPayload is the preferred way to create events
EventPayload = Event

# Type alias for event handlers
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Async event bus implementing pub/sub pattern.
    
    Features:
    - Async event handling
    - Priority-based event ordering
    - Wildcard subscriptions (subscribe to all events)
    - Error isolation (handler errors don't affect other handlers)
    - Event history for debugging
    """
    
    _instance: Optional['EventBus'] = None
    
    def __new__(cls) -> 'EventBus':
        """Singleton pattern - only one event bus per application."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._handlers: Dict[EventType, List[EventHandler]] = defaultdict(list)
        self._wildcard_handlers: List[EventHandler] = []
        self._event_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._processing_task: Optional[asyncio.Task] = None
        self._running = False
        self._event_history: List[Event] = []
        self._max_history_size = 1000
        self._lock = asyncio.Lock()
        self._initialized = True
        
        LOG.debug("EventBus initialized")
    
    def reset(self) -> None:
        """Reset the event bus state. Primarily for testing."""
        self._handlers.clear()
        self._wildcard_handlers.clear()
        self._event_history.clear()
        # Drain the queue
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
    
    async def start(self) -> None:
        """Start processing events from the queue."""
        if self._running:
            LOG.warning("EventBus already running")
            return
        
        self._running = True
        self._processing_task = asyncio.create_task(self._process_events())
        LOG.info("EventBus started")
    
    async def stop(self) -> None:
        """Stop processing events and clean up."""
        self._running = False
        
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
            self._processing_task = None
        
        LOG.info("EventBus stopped")
    
    def subscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Subscribe a handler to a specific event type.
        
        Args:
            event_type: The event type to subscribe to
            handler: Async function to call when event occurs
        """
        if handler not in self._handlers[event_type]:
            self._handlers[event_type].append(handler)
            LOG.debug(f"Subscribed handler to {event_type.name}")
    
    def subscribe_all(self, handler: EventHandler) -> None:
        """Subscribe a handler to all events (wildcard subscription).
        
        Args:
            handler: Async function to call for any event
        """
        if handler not in self._wildcard_handlers:
            self._wildcard_handlers.append(handler)
            LOG.debug("Subscribed wildcard handler")
    
    def unsubscribe(self, event_type: EventType, handler: EventHandler) -> None:
        """Unsubscribe a handler from a specific event type.
        
        Args:
            event_type: The event type to unsubscribe from
            handler: The handler to remove
        """
        if handler in self._handlers[event_type]:
            self._handlers[event_type].remove(handler)
            LOG.debug(f"Unsubscribed handler from {event_type.name}")
    
    def unsubscribe_all(self, handler: EventHandler) -> None:
        """Unsubscribe a handler from all events.
        
        Args:
            handler: The handler to remove
        """
        if handler in self._wildcard_handlers:
            self._wildcard_handlers.remove(handler)
        
        for event_type in self._handlers:
            if handler in self._handlers[event_type]:
                self._handlers[event_type].remove(handler)
    
    async def publish(self, event: Event) -> None:
        """Publish an event to the bus.
        
        If the bus is running, events are queued for processing.
        If not running, events are dispatched directly (for testing/simple use).
        
        Args:
            event: The event to publish
        """
        if self._running:
            # Queue-based processing
            priority_key = -event.priority
            await self._event_queue.put((priority_key, id(event), event))
            LOG.debug(f"Queued event: {event.event_type.name} (priority={event.priority})")
        else:
            # Direct dispatch (for testing or when bus isn't started)
            await self._dispatch_event(event)
            LOG.debug(f"Dispatched event: {event.event_type.name}")
    
    async def _dispatch_event(self, event: Event) -> None:
        """Dispatch an event to all registered handlers.
        
        Args:
            event: The event to dispatch
        """
        # Add to history
        await self._add_to_history(event)
        
        # Get handlers for this specific event type
        handlers = list(self._handlers.get(event.event_type, []))
        
        # Add wildcard handlers
        handlers.extend(self._wildcard_handlers)
        
        # Call all handlers
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                LOG.error(f"Handler error for {event.event_type.name}: {e}")
    
    async def emit(
        self,
        event_type: EventType,
        data: Optional[Dict[str, Any]] = None,
        priority: int = 0,
        source: Optional[str] = None
    ) -> None:
        """Convenience method to emit an event.
        
        Args:
            event_type: The type of event
            data: Event data payload
            priority: Event priority (higher = more important)
            source: Module that emitted the event
        """
        event = Event(
            event_type=event_type,
            data=data or {},
            priority=priority,
            source=source
        )
        await self.publish(event)
    
    async def _process_events(self) -> None:
        """Background task to process events from the queue."""
        while self._running:
            try:
                # Wait for an event with timeout to allow checking _running flag
                try:
                    _, _, event = await asyncio.wait_for(
                        self._event_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                # Add to history
                await self._add_to_history(event)
                
                # Get all handlers for this event type
                handlers = list(self._handlers.get(event.event_type, []))
                handlers.extend(self._wildcard_handlers)
                
                if not handlers:
                    LOG.debug(f"No handlers for event: {event.event_type.name}")
                    continue
                
                # Run all handlers concurrently with error isolation
                await self._run_handlers(handlers, event)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                LOG.exception(f"Error in event processing loop: {e}")
    
    async def _run_handlers(self, handlers: List[EventHandler], event: Event) -> None:
        """Run handlers with error isolation.
        
        Args:
            handlers: List of handlers to run
            event: The event to pass to handlers
        """
        tasks = []
        for handler in handlers:
            task = asyncio.create_task(self._safe_handler_call(handler, event))
            tasks.append(task)
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _safe_handler_call(self, handler: EventHandler, event: Event) -> None:
        """Call a handler with error catching.
        
        Args:
            handler: The handler to call
            event: The event to pass
        """
        try:
            await handler(event)
        except Exception as e:
            handler_name = getattr(handler, '__name__', str(handler))
            LOG.exception(
                f"Error in event handler '{handler_name}' for {event.event_type.name}: {e}"
            )
    
    async def _add_to_history(self, event: Event) -> None:
        """Add event to history with size limit.
        
        Args:
            event: The event to add
        """
        async with self._lock:
            self._event_history.append(event)
            
            # Trim history if too large
            if len(self._event_history) > self._max_history_size:
                self._event_history = self._event_history[-self._max_history_size:]
    
    def get_history(
        self,
        event_type: Optional[EventType] = None,
        limit: int = 100
    ) -> List[Event]:
        """Get event history, optionally filtered by type.
        
        Args:
            event_type: Filter by this event type (None = all)
            limit: Maximum number of events to return
        
        Returns:
            List of events, most recent first
        """
        history = self._event_history.copy()
        
        if event_type:
            history = [e for e in history if e.event_type == event_type]
        
        return history[-limit:][::-1]  # Most recent first
    
    def clear_history(self) -> None:
        """Clear the event history."""
        self._event_history.clear()
    
    @property
    def is_running(self) -> bool:
        """Check if the event bus is running."""
        return self._running
    
    @property
    def queue_size(self) -> int:
        """Get current size of the event queue."""
        return self._event_queue.qsize()


# Global event bus instance
_event_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """Get the global event bus instance.
    
    Returns:
        The singleton EventBus instance
    """
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


async def emit(
    event_type: EventType,
    data: Optional[Dict[str, Any]] = None,
    priority: int = 0,
    source: Optional[str] = None
) -> None:
    """Convenience function to emit an event to the global bus.
    
    Args:
        event_type: The type of event
        data: Event data payload
        priority: Event priority (higher = more important)
        source: Module that emitted the event
    """
    bus = get_event_bus()
    await bus.emit(event_type, data, priority, source)


def subscribe(event_type: EventType, handler: EventHandler) -> None:
    """Convenience function to subscribe to the global bus.
    
    Args:
        event_type: The event type to subscribe to
        handler: Async function to call when event occurs
    """
    bus = get_event_bus()
    bus.subscribe(event_type, handler)


def unsubscribe(event_type: EventType, handler: EventHandler) -> None:
    """Convenience function to unsubscribe from the global bus.
    
    Args:
        event_type: The event type to unsubscribe from
        handler: The handler to remove
    """
    bus = get_event_bus()
    bus.unsubscribe(event_type, handler)
