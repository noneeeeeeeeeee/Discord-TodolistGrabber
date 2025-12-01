import asyncio
import logging
import os
from typing import Dict

LOG = logging.getLogger(__name__)

class AutoplaySessionManager:
    """
    Manages ML session slots and enforces strict concurrency limits.
    
    Implements the "VIP Room" logic:
    - Max N concurrent guilds (default: 2)
    - Strict rejection if full (no degraded fallback)
    """
    def __init__(self, max_sessions: int = 2):
        self._max_sessions = max_sessions
        self._semaphore = asyncio.Semaphore(max_sessions)
        self._active_sessions: Dict[int, asyncio.Lock] = {}  # guild_id -> lock
        
        LOG.info(f"🎱 Session Manager initialized with {max_sessions} slots")
    
    def get_active_count(self) -> int:
        """Return the number of currently active sessions."""
        return len(self._active_sessions)
    
    def has_active_sessions(self) -> bool:
        """Check if any sessions are currently active."""
        return len(self._active_sessions) > 0
    
    def is_full(self) -> bool:
        """Check if all session slots are occupied."""
        return len(self._active_sessions) >= self._max_sessions
    
    async def acquire(self, guild_id: int) -> bool:
        """
        Try to acquire an ML session slot for this guild.
        Returns True if acquired (or already held), False if FULL.
        """
        # If guild already has a session, allow it
        if guild_id in self._active_sessions:
            return True
        
        # Check if semaphore is locked (full)
        if self._semaphore.locked():
            LOG.warning(f"⚠️ Guild {guild_id} denied autoplay (Slots full: {self._max_sessions}/{self._max_sessions}).")
            return False
        
        try:
            # Try to acquire non-blocking first (optimization)
            await self._semaphore.acquire()
            self._active_sessions[guild_id] = asyncio.Lock()
            LOG.info(f"✅ Guild {guild_id} acquired autoplay session (Active: {len(self._active_sessions)}/{self._max_sessions})")
            return True
        except Exception as e:
            LOG.error(f"❌ Error acquiring session for guild {guild_id}: {e}")
            return False
    
    async def release(self, guild_id: int) -> None:
        """
        Release the session slot for a guild.
        """
        if guild_id in self._active_sessions:
            del self._active_sessions[guild_id]
            self._semaphore.release()
            LOG.info(f"👋 Guild {guild_id} released autoplay session (Active: {len(self._active_sessions)}/{self._max_sessions})")
