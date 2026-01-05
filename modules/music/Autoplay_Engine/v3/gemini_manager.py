"""
V3 Autoplay Engine - Gemini Manager

Handles all interactions with Google's Gemini API for the librarian layer
of song analysis. Provides multi-key rotation, request batching, rate limiting,
exponential backoff, and grounding prioritization.

Key Features:
- Multi-API key rotation from GeminiApiKeys environment variable
- Request batching (splits 100 requests into 2 batches of 50)
- Exponential backoff for rate limiting and transient errors
- Google Search grounding for explicit_content, cultural_vibe, canonical_title
- Structured JSON output for consistent parsing
"""

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import aiohttp

from .constants import EventType, V3Config
from .event_bus import EventBus, EventPayload

logger = logging.getLogger(__name__)


class GroundingPriority(Enum):
    """Fields that should use Google Search grounding."""
    EXPLICIT_CONTENT = "explicit_content"
    CULTURAL_VIBE = "cultural_vibe"
    CANONICAL_TITLE = "canonical_title"


@dataclass
class GeminiResponse:
    """Response from Gemini API."""
    success: bool
    data: Optional[dict] = None
    error: Optional[str] = None
    grounded: bool = False
    usage: Optional[dict] = None


class GeminiManager:
    """
    Manages Gemini API interactions with key rotation, batching, and rate limiting.
    
    Loads API keys from GeminiApiKeys environment variable (comma-separated).
    Uses gemini-2.5-flash as the primary model with Google Search grounding
    for specific fields.
    """
    
    MODEL = "gemini-2.5-flash"
    BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"
    
    # Rate limiting configuration
    MAX_REQUESTS_PER_MINUTE = 60
    MAX_RETRIES = 3
    BASE_RETRY_DELAY = 1.0
    MAX_RETRY_DELAY = 32.0
    
    # Batching configuration
    BATCH_SIZE = 50
    
    def __init__(
        self,
        config: Optional[V3Config] = None,
        event_bus: Optional[EventBus] = None
    ):
        """
        Initialize Gemini manager.
        
        Args:
            config: V3 configuration
            event_bus: Event bus for notifications
        """
        self.config = config or V3Config()
        self.event_bus = event_bus or EventBus()
        
        # Load API keys from environment
        self._api_keys: list[str] = []
        self._current_key_index = 0
        self._load_api_keys()
        
        # Track key health
        self._key_failures: dict[int, int] = {}
        self._key_cooldowns: dict[int, float] = {}
        
        # Rate limiting
        self._request_times: list[float] = []
        self._rate_limit_lock = asyncio.Lock()
        
        # Session for HTTP requests
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Statistics
        self._stats = {
            "requests": 0,
            "successes": 0,
            "failures": 0,
            "retries": 0,
            "grounded_requests": 0,
            "tokens_used": 0
        }
        
        self._initialized = False
    
    def _load_api_keys(self) -> None:
        """Load API keys from environment variable."""
        keys_str = os.environ.get("GeminiApiKeys", "")
        
        if keys_str:
            self._api_keys = [k.strip() for k in keys_str.split(",") if k.strip()]
        
        if not self._api_keys:
            # Try single key format
            single_key = os.environ.get("GEMINI_API_KEY", "")
            if single_key:
                self._api_keys = [single_key]
        
        if self._api_keys:
            logger.info(f"Loaded {len(self._api_keys)} Gemini API key(s)")
        else:
            logger.warning("No Gemini API keys found in environment")
    
    async def initialize(self) -> None:
        """Initialize HTTP session and validate keys."""
        if self._initialized:
            return
        
        if not self._api_keys:
            raise RuntimeError("No Gemini API keys available")
        
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        )
        
        self._initialized = True
        logger.info("Gemini manager initialized")
    
    async def shutdown(self) -> None:
        """Clean up resources."""
        if self._session:
            await self._session.close()
            self._session = None
        
        self._initialized = False
        logger.info("Gemini manager shutdown complete")
    
    def _get_next_key(self) -> tuple[str, int]:
        """
        Get next available API key using round-robin with health checking.
        
        Returns:
            Tuple of (api_key, key_index)
        """
        now = time.time()
        attempts = 0
        
        while attempts < len(self._api_keys):
            idx = self._current_key_index
            self._current_key_index = (self._current_key_index + 1) % len(self._api_keys)
            
            # Check if key is in cooldown
            cooldown_until = self._key_cooldowns.get(idx, 0)
            if now < cooldown_until:
                attempts += 1
                continue
            
            # Check if key has too many failures
            failures = self._key_failures.get(idx, 0)
            if failures >= 5:
                # Reset after 5 minutes
                if now - cooldown_until > 300:
                    self._key_failures[idx] = 0
                else:
                    attempts += 1
                    continue
            
            return self._api_keys[idx], idx
        
        # All keys exhausted, use first one anyway
        logger.warning("All API keys in cooldown, forcing first key")
        return self._api_keys[0], 0
    
    def _mark_key_failure(self, key_index: int, retry_after: float = 60) -> None:
        """Mark an API key as having failed."""
        self._key_failures[key_index] = self._key_failures.get(key_index, 0) + 1
        self._key_cooldowns[key_index] = time.time() + retry_after
        logger.warning(f"API key {key_index} marked as failed (count: {self._key_failures[key_index]})")
    
    def _mark_key_success(self, key_index: int) -> None:
        """Reset failure count for successful key."""
        if key_index in self._key_failures:
            self._key_failures[key_index] = 0
    
    async def _wait_for_rate_limit(self) -> None:
        """Wait if necessary to respect rate limits."""
        async with self._rate_limit_lock:
            now = time.time()
            
            # Remove old requests outside the 1-minute window
            self._request_times = [t for t in self._request_times if now - t < 60]
            
            if len(self._request_times) >= self.MAX_REQUESTS_PER_MINUTE:
                # Wait until oldest request is outside window
                wait_time = 60 - (now - self._request_times[0]) + 0.1
                if wait_time > 0:
                    logger.debug(f"Rate limit: waiting {wait_time:.1f}s")
                    await asyncio.sleep(wait_time)
            
            self._request_times.append(time.time())
    
    async def _make_request(
        self,
        prompt: str,
        system_instruction: Optional[str] = None,
        use_grounding: bool = False,
        json_schema: Optional[dict] = None
    ) -> GeminiResponse:
        """
        Make a single request to Gemini API with retry logic.
        
        Args:
            prompt: User prompt text
            system_instruction: System instruction for context
            use_grounding: Whether to use Google Search grounding
            json_schema: JSON schema for structured output
            
        Returns:
            GeminiResponse with result or error
        """
        await self.initialize()
        await self._wait_for_rate_limit()
        
        api_key, key_index = self._get_next_key()
        
        url = f"{self.BASE_URL}/{self.MODEL}:generateContent?key={api_key}"
        
        # Build request body
        contents = [{"parts": [{"text": prompt}]}]
        
        generation_config = {
            "temperature": 0.7,
            "topP": 0.95,
            "maxOutputTokens": 8192
        }
        
        # Add JSON schema if provided
        if json_schema:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = json_schema
        
        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config
        }
        
        # Add system instruction
        if system_instruction:
            body["systemInstruction"] = {
                "parts": [{"text": system_instruction}]
            }
        
        # Add grounding configuration
        if use_grounding:
            body["tools"] = [{"googleSearch": {}}]
            self._stats["grounded_requests"] += 1
        
        # Retry loop with exponential backoff
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                self._stats["requests"] += 1
                
                async with self._session.post(url, json=body) as response:
                    if response.status == 200:
                        data = await response.json()
                        self._mark_key_success(key_index)
                        self._stats["successes"] += 1
                        
                        # Extract usage info
                        usage = data.get("usageMetadata", {})
                        if usage:
                            self._stats["tokens_used"] += usage.get("totalTokenCount", 0)
                        
                        # Extract text response
                        try:
                            text = data["candidates"][0]["content"]["parts"][0]["text"]
                            
                            # Parse JSON if schema was provided
                            if json_schema:
                                parsed = json.loads(text)
                                return GeminiResponse(
                                    success=True,
                                    data=parsed,
                                    grounded=use_grounding,
                                    usage=usage
                                )
                            
                            return GeminiResponse(
                                success=True,
                                data={"text": text},
                                grounded=use_grounding,
                                usage=usage
                            )
                            
                        except (KeyError, IndexError, json.JSONDecodeError) as e:
                            return GeminiResponse(
                                success=False,
                                error=f"Failed to parse response: {e}"
                            )
                    
                    elif response.status == 429:
                        # Rate limited
                        retry_after = float(response.headers.get("Retry-After", 60))
                        self._mark_key_failure(key_index, retry_after)
                        last_error = "Rate limited"
                        
                    elif response.status == 403:
                        # API key issue
                        self._mark_key_failure(key_index, 300)
                        last_error = "API key forbidden"
                        
                    elif response.status >= 500:
                        # Server error, retry
                        last_error = f"Server error: {response.status}"
                        
                    else:
                        # Other error
                        error_text = await response.text()
                        last_error = f"API error {response.status}: {error_text[:200]}"
                        self._stats["failures"] += 1
                        return GeminiResponse(success=False, error=last_error)
                
            except asyncio.TimeoutError:
                last_error = "Request timed out"
            except aiohttp.ClientError as e:
                last_error = f"Network error: {e}"
            
            # Exponential backoff for retry
            if attempt < self.MAX_RETRIES - 1:
                self._stats["retries"] += 1
                delay = min(
                    self.BASE_RETRY_DELAY * (2 ** attempt) + random.uniform(0, 1),
                    self.MAX_RETRY_DELAY
                )
                logger.warning(f"Retrying after {delay:.1f}s (attempt {attempt + 1}): {last_error}")
                await asyncio.sleep(delay)
                
                # Try a different key
                api_key, key_index = self._get_next_key()
                url = f"{self.BASE_URL}/{self.MODEL}:generateContent?key={api_key}"
        
        self._stats["failures"] += 1
        return GeminiResponse(success=False, error=last_error)
    
    async def analyze_song(
        self,
        title: str,
        artist: str,
        album: Optional[str] = None,
        audio_features: Optional[dict] = None,
        semantic_tags: Optional[list[str]] = None
    ) -> GeminiResponse:
        """
        Analyze a song using Gemini with grounding for specific fields.
        
        Args:
            title: Song title
            artist: Artist name
            album: Album name (optional)
            audio_features: Librosa analysis results
            semantic_tags: EfficientAT instrument/sound tags
            
        Returns:
            GeminiResponse with librarian analysis
        """
        # Build context from available data
        context_parts = [
            f"Song: {title}",
            f"Artist: {artist}"
        ]
        
        if album:
            context_parts.append(f"Album: {album}")
        
        if audio_features:
            context_parts.append(f"Audio features: BPM={audio_features.get('bpm')}, Key={audio_features.get('key')}")
        
        if semantic_tags:
            context_parts.append(f"Detected sounds: {', '.join(semantic_tags[:10])}")
        
        context = "\n".join(context_parts)
        
        system_instruction = """You are a music librarian and analyst. Analyze the given song and provide detailed metadata.
Focus on accuracy and specificity. Use your knowledge of music history, genres, and cultural context.
For explicit content, cultural vibes, and canonical titles, search for verified information."""
        
        prompt = f"""Analyze this song and provide detailed metadata:

{context}

Provide your analysis in the specified JSON format. Be specific about genres (use subgenres where appropriate),
moods (use nuanced descriptors), and themes (identify specific lyrical/musical themes).

For explicit_content, verify if the song contains explicit lyrics or themes.
For cultural_vibe, identify the cultural/regional influences and era.
For canonical_title, provide the official/standardized title if different from the provided one."""
        
        # JSON schema for structured output
        json_schema = {
            "type": "object",
            "properties": {
                "genres": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of genres and subgenres, most specific first"
                },
                "moods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Emotional qualities and atmosphere"
                },
                "themes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Lyrical and musical themes"
                },
                "energy_level": {
                    "type": "number",
                    "description": "Energy level from 0.0 (calm) to 1.0 (intense)"
                },
                "danceability": {
                    "type": "number",
                    "description": "How danceable from 0.0 to 1.0"
                },
                "explicit_content": {
                    "type": "boolean",
                    "description": "Whether song contains explicit content"
                },
                "cultural_vibe": {
                    "type": "string",
                    "description": "Cultural/regional influences and era"
                },
                "canonical_title": {
                    "type": "string",
                    "description": "Official/standardized song title"
                },
                "similar_artists": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Artists with similar sound"
                },
                "recommendation_tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags useful for recommendations"
                }
            },
            "required": ["genres", "moods", "energy_level"]
        }
        
        # Use grounding for better accuracy on specific fields
        return await self._make_request(
            prompt=prompt,
            system_instruction=system_instruction,
            use_grounding=True,
            json_schema=json_schema
        )
    
    async def batch_analyze_songs(
        self,
        songs: list[dict[str, Any]]
    ) -> list[GeminiResponse]:
        """
        Analyze multiple songs with batching.
        
        Splits requests into batches of 50 to avoid overwhelming the API.
        
        Args:
            songs: List of song dictionaries with title, artist, etc.
            
        Returns:
            List of GeminiResponse objects in same order as input
        """
        results = []
        
        # Split into batches
        for i in range(0, len(songs), self.BATCH_SIZE):
            batch = songs[i:i + self.BATCH_SIZE]
            
            # Process batch concurrently but with rate limiting
            batch_tasks = [
                self.analyze_song(
                    title=song.get("title", ""),
                    artist=song.get("artist", ""),
                    album=song.get("album"),
                    audio_features=song.get("audio_features"),
                    semantic_tags=song.get("semantic_tags")
                )
                for song in batch
            ]
            
            batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
            
            for result in batch_results:
                if isinstance(result, Exception):
                    results.append(GeminiResponse(
                        success=False,
                        error=str(result)
                    ))
                else:
                    results.append(result)
            
            # Small delay between batches
            if i + self.BATCH_SIZE < len(songs):
                await asyncio.sleep(1.0)
        
        # Publish batch completion event
        await self.event_bus.publish(EventPayload(
            event_type=EventType.ANALYSIS_COMPLETE,
            data={
                "batch_size": len(songs),
                "successes": sum(1 for r in results if r.success),
                "failures": sum(1 for r in results if not r.success)
            }
        ))
        
        return results
    
    async def get_recommendations_prompt(
        self,
        context: str,
        preferences: dict[str, Any],
        constraints: Optional[dict[str, Any]] = None
    ) -> GeminiResponse:
        """
        Generate recommendation reasoning using Gemini.
        
        Used for hybrid recommendation logic where Gemini provides
        reasoning about which songs to recommend.
        
        Args:
            context: Current session context (recent songs, skips, etc.)
            preferences: User preference profile
            constraints: Any constraints (avoid genres, required features)
            
        Returns:
            GeminiResponse with recommendation reasoning
        """
        system_instruction = """You are a music recommendation engine. Based on the listening context
and preferences, provide reasoning about what kind of music to recommend next.
Consider variety, flow, and the listener's apparent mood."""
        
        prompt_parts = [
            "Listening Context:",
            context,
            "",
            "Preferences:",
            json.dumps(preferences, indent=2)
        ]
        
        if constraints:
            prompt_parts.extend([
                "",
                "Constraints:",
                json.dumps(constraints, indent=2)
            ])
        
        prompt_parts.extend([
            "",
            "Based on this context, describe what kind of song would be a good next recommendation.",
            "Focus on: genres, energy level, mood progression, and any artists that would fit well."
        ])
        
        json_schema = {
            "type": "object",
            "properties": {
                "recommended_genres": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "target_energy": {
                    "type": "number",
                    "description": "Target energy level 0.0-1.0"
                },
                "target_moods": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "suggested_artists": {
                    "type": "array",
                    "items": {"type": "string"}
                },
                "reasoning": {
                    "type": "string",
                    "description": "Brief explanation of recommendation logic"
                },
                "avoid": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Genres/artists to avoid based on context"
                }
            },
            "required": ["recommended_genres", "target_energy", "reasoning"]
        }
        
        return await self._make_request(
            prompt="\n".join(prompt_parts),
            system_instruction=system_instruction,
            use_grounding=False,  # No grounding needed for recommendations
            json_schema=json_schema
        )
    
    async def get_stats(self) -> dict[str, Any]:
        """Get API usage statistics."""
        return {
            **self._stats,
            "available_keys": len(self._api_keys),
            "keys_in_cooldown": sum(
                1 for idx in range(len(self._api_keys))
                if time.time() < self._key_cooldowns.get(idx, 0)
            )
        }


# Singleton instance
_gemini_manager: Optional[GeminiManager] = None


def get_gemini_manager() -> GeminiManager:
    """Get global Gemini manager instance."""
    global _gemini_manager
    if _gemini_manager is None:
        _gemini_manager = GeminiManager()
    return _gemini_manager
