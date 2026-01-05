"""
V3 Autoplay Engine - Recommender (Head Chef)

Central orchestration module that coordinates all recommendation strategies:
- Cold Start: Last.fm similarity + safe genre picks
- Warm: Hybrid Last.fm + light CF + context preferences
- Hot: Full CF + context + novelty nudges
- Extended: Aggressive exploration with daydreamer integration

This is the "head chef" that combines signals from all other modules
to produce final song recommendations.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

from .cache_manager import CacheManager, get_cache_manager
from .vector_search_index import VectorSearcher, get_vector_searcher
from .collaborative_recommender import CollaborativeRecommender, get_collaborative_recommender
from .constants import AnalysisPriority, EventType, SessionState, SongMetadata, V3Config
from .context_analyzer import ContextAnalyzer, get_context_analyzer
from .event_bus import EventBus, EventPayload
from .gemini_manager import GeminiManager, get_gemini_manager
from .mappings import MappingsManager, SongIdentifier, get_mappings_manager
from .novelty_controller import NoveltyController, NoveltyNudge, get_novelty_controller
from .song_analyzer import SongAnalyzer, get_song_analyzer

logger = logging.getLogger(__name__)


@dataclass
class RecommendationCandidate:
    """A candidate song for recommendation."""
    identifier: SongIdentifier
    metadata: Optional[SongMetadata]
    source: str  # "lastfm", "vector_search", "behavioral", "gemini", "daydreamer", "cache"
    base_score: float
    adjustments: dict[str, float]
    
    @property
    def final_score(self) -> float:
        """Calculate final score with all adjustments."""
        return self.base_score + sum(self.adjustments.values())


@dataclass
class Recommendation:
    """Final recommendation to play."""
    identifier: SongIdentifier
    score: float
    reason: str
    source: str


class Recommender:
    """
    Head chef: orchestrates all recommendation strategies.
    
    Strategy by Session State:
    
    COLD (1-10 songs):
    - Primary: Last.fm track.getSimilar
    - Secondary: Seed genre expansion via Gemini
    - Fallback: Random from cache
    - Analysis: Immediate for played songs
    
    WARM (11-25 songs):
    - Primary: Last.fm + Context preferences
    - Secondary: Light CF if available (500+ songs)
    - Novelty: Genre stagnation checks
    - Analysis: High priority for queue
    
    HOT (25+ songs):
    - Primary: Full collaborative filtering
    - Secondary: Last.fm weighted by preferences
    - Novelty: Full nudge suite
    - Analysis: Background for exploration
    
    EXTENDED (25+ songs, special mode):
    - Same as HOT but with daydreamer integration
    - More aggressive exploration
    - Genre distribution balancing
    """
    
    # Strategy weights by state
    STRATEGY_WEIGHTS = {
        SessionState.COLD: {
            "lastfm": 0.7,
            "cache": 0.2,
            "gemini": 0.1
        },
        SessionState.WARM: {
            "lastfm": 0.5,
            "cf": 0.3,
            "context": 0.15,
            "cache": 0.05
        },
        SessionState.HOT: {
            "vector_search": 0.3,    # Content similarity
            "behavioral": 0.2,       # Transition patterns
            "lastfm": 0.25,
            "context": 0.15,
            "novelty": 0.1
        },
        SessionState.EXTENDED: {
            "vector_search": 0.25,
            "behavioral": 0.2,
            "lastfm": 0.2,
            "context": 0.15,
            "novelty": 0.1,
            "daydreamer": 0.1
        }
    }
    
    def __init__(
        self,
        config: Optional[V3Config] = None,
        cache: Optional[CacheManager] = None,
        mappings: Optional[MappingsManager] = None,
        analyzer: Optional[SongAnalyzer] = None,
        context: Optional[ContextAnalyzer] = None,
        novelty: Optional[NoveltyController] = None,
        vector_search: Optional[VectorSearcher] = None,
        collaborative: Optional[CollaborativeRecommender] = None,
        gemini: Optional[GeminiManager] = None,
        event_bus: Optional[EventBus] = None
    ):
        """
        Initialize recommender with all dependencies.
        
        Dependencies:
        - vector_search: Content-based similarity (audio features)
        - collaborative: Behavioral recommendations (transitions, user prefs)
        """
        self.config = config or V3Config()
        self.cache = cache or get_cache_manager()
        self.mappings = mappings or get_mappings_manager()
        self.analyzer = analyzer or get_song_analyzer()
        self.context = context or get_context_analyzer()
        self.novelty = novelty or get_novelty_controller()
        self.vector_search = vector_search or get_vector_searcher()
        self.collaborative = collaborative or get_collaborative_recommender()
        self.gemini = gemini or get_gemini_manager()
        self.event_bus = event_bus or EventBus()
        
        # Daydreamer reference (set externally)
        self._daydreamer = None
        
        # Statistics
        self._stats = {
            "recommendations": 0,
            "by_source": {},
            "by_state": {}
        }
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """Initialize all dependencies."""
        if self._initialized:
            return
        
        await asyncio.gather(
            self.cache.initialize(),
            self.mappings.initialize(),
            self.analyzer.initialize(),
            self.context.initialize(),
            self.novelty.initialize(),
            self.vector_search.initialize(),
            self.collaborative.initialize(),
            self.gemini.initialize()
        )
        
        self._initialized = True
        logger.info("Recommender (head chef) initialized")
    
    async def shutdown(self) -> None:
        """Shutdown all dependencies."""
        await asyncio.gather(
            self.cache.shutdown(),
            self.analyzer.shutdown(),
            self.context.shutdown(),
            self.novelty.shutdown(),
            self.vector_search.shutdown(),
            self.collaborative.shutdown(),
            self.gemini.shutdown()
        )
        self._initialized = False
    
    def set_daydreamer(self, daydreamer: Any) -> None:
        """Set daydreamer reference for extended sessions."""
        self._daydreamer = daydreamer
    
    async def get_recommendation(
        self,
        session_id: str,
        guild_id: str,
        seed_songs: list[SongIdentifier],
        exclude_songs: Optional[set[str]] = None,
        count: int = 1
    ) -> list[Recommendation]:
        """
        Get song recommendations for a session.
        
        Args:
            session_id: Session identifier
            guild_id: Discord guild ID
            seed_songs: Recent songs to base recommendations on
            exclude_songs: Songs to exclude (recently played, in queue)
            count: Number of recommendations to return
            
        Returns:
            List of Recommendation objects
        """
        await self.initialize()
        
        exclude_songs = exclude_songs or set()
        
        # Get or create session profile
        session = self.context.get_or_create_session(session_id, guild_id)
        state = session.state
        
        # Get session context
        ctx = self.context.get_session_context(session_id)
        
        # Check if reanalysis is needed
        if ctx and ctx.get("needs_reanalysis"):
            logger.info(f"Context reanalysis triggered for session {session_id}")
            # This would trigger buffer recalculation in buffer_manager
            await self.event_bus.publish(EventPayload(
                event_type=EventType.CONTEXT_SHIFT,
                data={"session_id": session_id, "reason": "skip_pattern"}
            ))
        
        # Get novelty nudges
        nudges = self.novelty.get_nudges(
            session_id,
            state,
            len(session.playback_history),
            session.skip_rate
        )
        
        # Gather candidates from all sources
        candidates = await self._gather_candidates(
            session_id,
            state,
            seed_songs,
            exclude_songs,
            ctx,
            count * 5  # Get more candidates for scoring
        )
        
        # Score and rank candidates
        scored = self._score_candidates(candidates, state, ctx, nudges)
        
        # Select top candidates
        recommendations = self._select_recommendations(scored, count)
        
        # Queue selected songs for analysis
        for rec in recommendations:
            await self.analyzer.enqueue(
                rec.identifier,
                priority=AnalysisPriority.HIGH
            )
        
        # Update stats
        self._stats["recommendations"] += len(recommendations)
        self._stats["by_state"][state.value] = \
            self._stats["by_state"].get(state.value, 0) + len(recommendations)
        
        for rec in recommendations:
            self._stats["by_source"][rec.source] = \
                self._stats["by_source"].get(rec.source, 0) + 1
        
        return recommendations
    
    async def _gather_candidates(
        self,
        session_id: str,
        state: SessionState,
        seed_songs: list[SongIdentifier],
        exclude_songs: set[str],
        context: Optional[dict],
        count: int
    ) -> list[RecommendationCandidate]:
        """Gather candidates from all sources based on session state."""
        candidates = []
        weights = self.STRATEGY_WEIGHTS.get(state, self.STRATEGY_WEIGHTS[SessionState.COLD])
        
        # Parallelize candidate gathering
        tasks = []
        
        # Last.fm similarity
        if weights.get("lastfm", 0) > 0 and seed_songs:
            tasks.append(self._get_lastfm_candidates(
                seed_songs[-3:],  # Use last 3 songs as seeds
                exclude_songs,
                count
            ))
        
        # Collaborative filtering (using vector search for content similarity)
        if weights.get("cf", 0) > 0 and self.vector_search.is_active:
            seed_ids = [s.primary_id for s in seed_songs if s.primary_id]
            if seed_ids:
                tasks.append(self._get_cf_candidates(
                    seed_ids[-5:],
                    exclude_songs,
                    count
                ))
        
        # Cache-based fallback
        if weights.get("cache", 0) > 0:
            tasks.append(self._get_cache_candidates(
                context,
                exclude_songs,
                count // 2
            ))
        
        # Daydreamer candidates for extended sessions
        if weights.get("daydreamer", 0) > 0 and self._daydreamer:
            tasks.append(self._get_daydreamer_candidates(
                session_id,
                exclude_songs,
                count // 3
            ))
        
        # Wait for all sources
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, list):
                candidates.extend(result)
            elif isinstance(result, Exception):
                logger.warning(f"Candidate gathering error: {result}")
        
        return candidates
    
    async def _get_lastfm_candidates(
        self,
        seed_songs: list[SongIdentifier],
        exclude_songs: set[str],
        count: int
    ) -> list[RecommendationCandidate]:
        """Get candidates from Last.fm similar tracks."""
        candidates = []
        
        for seed in seed_songs:
            if not seed.title or not seed.artist:
                continue
            
            similar = await self.mappings.get_similar_tracks(
                title=seed.title,
                artist=seed.artist,
                limit=100  # Batch request
            )
            
            for track in similar:
                if track.primary_id and track.primary_id in exclude_songs:
                    continue
                
                # Resolve to get full info
                resolved = await self.mappings.resolve_song(
                    title=track.title,
                    artist=track.artist
                )
                
                if resolved and resolved.primary_id not in exclude_songs:
                    # Get metadata if available
                    metadata = None
                    if resolved.primary_id:
                        metadata = await self.cache.get_metadata(resolved.primary_id)
                    
                    candidates.append(RecommendationCandidate(
                        identifier=resolved,
                        metadata=metadata,
                        source="lastfm",
                        base_score=0.7 - (len(candidates) * 0.01),  # Decay by position
                        adjustments={}
                    ))
                    
                    if len(candidates) >= count:
                        break
            
            if len(candidates) >= count:
                break
        
        return candidates
    
    async def _get_cf_candidates(
        self,
        seed_ids: list[str],
        exclude_songs: set[str],
        count: int
    ) -> list[RecommendationCandidate]:
        """Get candidates from vector search (content-based similarity)."""
        candidates = []
        
        results = await self.vector_search.find_similar_batch(
            song_ids=seed_ids,
            k=count,
            exclude=exclude_songs
        )
        
        for result in results:
            if result.metadata:
                identifier = SongIdentifier(
                    deezer_id=result.song_id if result.song_id.isdigit() else None,
                    title=result.metadata.title,
                    artist=result.metadata.artist,
                    album=result.metadata.album,
                    preview_url=result.metadata.preview_url
                )
                
                candidates.append(RecommendationCandidate(
                    identifier=identifier,
                    metadata=result.metadata,
                    source="cf",
                    base_score=result.score,
                    adjustments={}
                ))
        
        return candidates
    
    async def _get_cache_candidates(
        self,
        context: Optional[dict],
        exclude_songs: set[str],
        count: int
    ) -> list[RecommendationCandidate]:
        """Get candidates from cache based on context."""
        candidates = []
        
        # Search by features from context
        bpm_range = None
        genres = None
        
        if context:
            avg_bpm = context.get("avg_bpm", 120)
            bpm_range = (avg_bpm - 20, avg_bpm + 20)
            
            top_genres = context.get("top_genres", [])
            if top_genres:
                genres = [g[0] for g in top_genres[:3]]
        
        results = await self.cache.search_by_features(
            bpm_range=bpm_range,
            genres=genres,
            limit=count * 2
        )
        
        for metadata in results:
            if metadata.song_id in exclude_songs:
                continue
            
            identifier = SongIdentifier(
                deezer_id=metadata.song_id,
                title=metadata.title,
                artist=metadata.artist,
                album=metadata.album,
                preview_url=metadata.preview_url
            )
            
            candidates.append(RecommendationCandidate(
                identifier=identifier,
                metadata=metadata,
                source="cache",
                base_score=0.5,
                adjustments={}
            ))
            
            if len(candidates) >= count:
                break
        
        return candidates
    
    async def _get_daydreamer_candidates(
        self,
        session_id: str,
        exclude_songs: set[str],
        count: int
    ) -> list[RecommendationCandidate]:
        """Get exploration candidates from daydreamer."""
        if not self._daydreamer:
            return []
        
        # Get suggestions from daydreamer
        suggestions = await self._daydreamer.get_suggestions(
            session_id,
            count=count,
            exclude=exclude_songs
        )
        
        candidates = []
        for suggestion in suggestions:
            candidates.append(RecommendationCandidate(
                identifier=suggestion.identifier,
                metadata=suggestion.metadata,
                source="daydreamer",
                base_score=0.6,  # Exploration bonus
                adjustments={"exploration": 0.1}
            ))
        
        return candidates
    
    def _score_candidates(
        self,
        candidates: list[RecommendationCandidate],
        state: SessionState,
        context: Optional[dict],
        nudges: list[NoveltyNudge]
    ) -> list[RecommendationCandidate]:
        """Score and adjust candidates based on context and nudges."""
        weights = self.STRATEGY_WEIGHTS.get(state, {})
        
        for candidate in candidates:
            # Apply source weight
            source_weight = weights.get(candidate.source, 0.1)
            candidate.adjustments["source_weight"] = source_weight * 0.3
            
            # Apply context adjustments
            if context and candidate.metadata:
                ctx_adj = self._calculate_context_adjustment(
                    candidate.metadata, context
                )
                candidate.adjustments["context"] = ctx_adj
            
            # Apply novelty nudges
            for nudge in nudges:
                nudge_adj = self._apply_nudge(candidate, nudge)
                if nudge_adj != 0:
                    candidate.adjustments[f"nudge_{nudge.nudge_type}"] = nudge_adj
        
        # Sort by final score
        candidates.sort(key=lambda c: c.final_score, reverse=True)
        
        return candidates
    
    def _calculate_context_adjustment(
        self,
        metadata: SongMetadata,
        context: dict
    ) -> float:
        """Calculate score adjustment based on user context/preferences."""
        adjustment = 0.0
        
        if not metadata.librarian_info:
            return adjustment
        
        # Genre match bonus
        top_genres = [g[0] for g in context.get("top_genres", [])]
        for genre in (metadata.librarian_info.genres or []):
            if genre in top_genres:
                adjustment += 0.15
                break
        
        # Avoided genre penalty
        avoided = context.get("avoided_genres", [])
        for genre in (metadata.librarian_info.genres or []):
            if genre in avoided:
                adjustment -= 0.4
                break
        
        # Energy match
        avg_energy = context.get("avg_energy", 0.5)
        song_energy = metadata.librarian_info.energy_level or 0.5
        energy_diff = abs(song_energy - avg_energy)
        if energy_diff < 0.2:
            adjustment += 0.1
        elif energy_diff > 0.4:
            adjustment -= 0.1
        
        # Artist preference
        top_artists = [a[0] for a in context.get("top_artists", [])]
        if metadata.artist in top_artists:
            adjustment += 0.2
        
        return adjustment
    
    def _apply_nudge(
        self,
        candidate: RecommendationCandidate,
        nudge: NoveltyNudge
    ) -> float:
        """Apply a novelty nudge to a candidate."""
        if not candidate.metadata:
            return 0.0
        
        meta = candidate.metadata
        
        if nudge.nudge_type == "genre" and nudge.target_value:
            # Boost target genre
            if meta.librarian_info:
                if nudge.target_value in (meta.librarian_info.genres or []):
                    return nudge.strength * 0.3
        
        elif nudge.nudge_type == "artist":
            # Penalize same artist
            if meta.artist == nudge.target_value:
                return -nudge.strength * 0.5
        
        elif nudge.nudge_type == "artist_cooldown":
            # Block artists in cooldown
            if nudge.target_value and meta.artist in nudge.target_value:
                return -1.0
        
        elif nudge.nudge_type == "energy":
            # Boost songs in target energy range
            if meta.librarian_info and nudge.target_range:
                energy = meta.librarian_info.energy_level or 0.5
                if nudge.target_range[0] <= energy <= nudge.target_range[1]:
                    return nudge.strength * 0.2
        
        elif nudge.nudge_type == "explore":
            # Small boost for exploration
            return nudge.strength * 0.1
        
        return 0.0
    
    def _select_recommendations(
        self,
        candidates: list[RecommendationCandidate],
        count: int
    ) -> list[Recommendation]:
        """Select final recommendations from scored candidates."""
        if not candidates:
            return []
        
        recommendations = []
        seen_artists = set()
        
        for candidate in candidates:
            if len(recommendations) >= count:
                break
            
            # Avoid recommending multiple songs from same artist
            if candidate.identifier.artist in seen_artists:
                continue
            
            # Skip if score is too low
            if candidate.final_score < 0.1:
                continue
            
            seen_artists.add(candidate.identifier.artist)
            
            # Build reason string
            reason = self._build_reason(candidate)
            
            recommendations.append(Recommendation(
                identifier=candidate.identifier,
                score=candidate.final_score,
                reason=reason,
                source=candidate.source
            ))
        
        return recommendations
    
    def _build_reason(self, candidate: RecommendationCandidate) -> str:
        """Build human-readable reason for recommendation."""
        parts = []
        
        if candidate.source == "lastfm":
            parts.append("Similar to recent plays")
        elif candidate.source == "cf":
            parts.append("Matches your taste profile")
        elif candidate.source == "cache":
            parts.append("From your preferred styles")
        elif candidate.source == "daydreamer":
            parts.append("Exploration pick")
        
        # Add adjustment reasons
        if "context" in candidate.adjustments and candidate.adjustments["context"] > 0.1:
            parts.append("good genre match")
        
        if any("nudge" in k for k in candidate.adjustments):
            parts.append("variety pick")
        
        return ", ".join(parts) if parts else "Recommended"
    
    async def get_stats(self) -> dict[str, Any]:
        """Get recommendation statistics."""
        return {
            **self._stats,
            "cache_stats": await self.cache.get_stats(),
            "vector_search_stats": self.vector_search.get_stats() if hasattr(self.vector_search, 'get_stats') else {},
            "collaborative_stats": self.collaborative.get_stats() if hasattr(self.collaborative, 'get_stats') else {},
            "analyzer_queue": await self.analyzer.get_queue_status()
        }


# Singleton instance
_recommender: Optional[Recommender] = None


def get_recommender() -> Recommender:
    """Get global recommender instance."""
    global _recommender
    if _recommender is None:
        _recommender = Recommender()
    return _recommender
