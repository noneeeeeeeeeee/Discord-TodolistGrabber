import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .collaborative_matrix import CollaborativeMatrix

LOG = logging.getLogger(__name__)

_DEFAULT_COLLAB_WEIGHT = 0.3
# Collaborative filtering disabled by default until ML training is complete
_COLLABORATIVE_FILTERING_ENABLED = False  # Set to True in future when ready


@dataclass
class CandidateFeatures:
    track_id: str
    artist: str
    title: str
    content_similarity: float
    session_similarity: float
    novelty: float
    quality: float
    feedback_multiplier: float = 1.0
    diversity_penalty: float = 1.0
    extra_weight: float = 1.0
    mood_vector: Optional[Sequence[float]] = None
    mood_label: Optional[str] = None
    # Phase 3: Enhanced scoring fields
    genres: Optional[List[str]] = None  # For genre coherence scoring
    energy: Optional[float] = None  # For energy flow scoring (0.0-1.0)


@dataclass
class ScoredCandidate:
    track_id: str
    artist: str
    title: str
    score: float
    breakdown: Dict[str, float]


class ContextualRecommender:
    """Scores candidates using content + collaborative + mood blending."""

    def __init__(self, collaborative: CollaborativeMatrix) -> None:
        self._collaborative = collaborative
        self._collab_toggles: Dict[str, bool] = {}
        self._collab_weight_overrides: Dict[str, float] = {}

    def set_collaborative_enabled(self, guild_id: int | str, enabled: bool) -> None:
        key = self._guild_key(guild_id)
        self._collab_toggles[key] = enabled

    def set_collaborative_weight(self, guild_id: int | str, weight: float) -> None:
        key = self._guild_key(guild_id)
        self._collab_weight_overrides[key] = max(0.0, min(1.0, weight))

    def is_collaborative_enabled(self, guild_id: int | str) -> bool:
        """Check if collaborative filtering is enabled (globally disabled until ML ready)."""
        if not _COLLABORATIVE_FILTERING_ENABLED:
            return False  # Global disable until future ML training
        key = self._guild_key(guild_id)
        return self._collab_toggles.get(key, True)

    def score_candidates(
        self,
        guild_id: int | str,
        candidates: Sequence[CandidateFeatures],
        *,
        seed_track_ids: Optional[Iterable[str]] = None,
        session_mood_vector: Optional[Sequence[float]] = None,
        target_mood: Optional[str] = None,
        # Phase 3: Enhanced scoring parameters
        session_focus_genres: Optional[List[str]] = None,
        liked_mood_vector: Optional[Sequence[float]] = None,
        energy_trend: float = 0.0,
        last_energy: Optional[float] = None,
    ) -> List[ScoredCandidate]:
        if not candidates:
            return []

        collaborative_ready = self.is_collaborative_enabled(guild_id)
        if collaborative_ready:
            snapshot_available = self._collaborative.snapshot_metadata().get(
                "available"
            )
            collaborative_ready = bool(snapshot_available)

        seeds = [track_id for track_id in (seed_track_ids or []) if track_id]
        collaborative_weight = self._collab_weight_overrides.get(
            self._guild_key(guild_id),
            _DEFAULT_COLLAB_WEIGHT,
        )

        session_vector = list(session_mood_vector) if session_mood_vector else None
        liked_vector = list(liked_mood_vector) if liked_mood_vector else None
        focus_genres_set = set(g.lower() for g in (session_focus_genres or []))

        scored: List[ScoredCandidate] = []
        for candidate in candidates:
            content_similarity = self._clamp01(candidate.content_similarity)
            session_similarity = self._clamp01(candidate.session_similarity)
            novelty = self._clamp01(candidate.novelty)
            quality = self._clamp01(candidate.quality)

            # Phase 3 Task 4.3: Updated mood alignment with safe anchor (60% weight to liked tracks)
            mood_alignment = 0.5
            mood_distance_avg = None
            mood_distance_liked = None

            if session_vector and candidate.mood_vector:
                mood_distance_avg = self._mood_distance(
                    session_vector, candidate.mood_vector
                )

            if liked_vector and candidate.mood_vector:
                mood_distance_liked = self._mood_distance(
                    liked_vector, candidate.mood_vector
                )

            # Blend session average (40%) with liked anchor (60%)
            if mood_distance_avg is not None and mood_distance_liked is not None:
                mood_alignment = self._clamp01(
                    0.4 * (1.0 - mood_distance_avg) + 0.6 * (1.0 - mood_distance_liked)
                )
            elif mood_distance_avg is not None:
                mood_alignment = self._clamp01(1.0 - mood_distance_avg)
            elif mood_distance_liked is not None:
                mood_alignment = self._clamp01(1.0 - mood_distance_liked)

            novelty_term = novelty
            if mood_distance_avg is not None:
                novelty_term = 0.6 * novelty + 0.4 * mood_distance_avg

            if target_mood and candidate.mood_label:
                if candidate.mood_label.lower() == target_mood.lower():
                    mood_alignment = self._clamp01(mood_alignment + 0.1)

            # Phase 3 Task 4.1: Genre coherence scoring (25% weight)
            genre_coherence = self._score_genre_coherence(
                candidate.genres or [], focus_genres_set
            )

            # Phase 3 Task 4.2: Energy flow scoring (10% weight)
            energy_flow = self._score_energy_flow(
                candidate.energy, last_energy, energy_trend
            )

            # Phase 3 Task 4.4: New score composition
            # 35% mood + 25% genre + 10% energy + 30% collaborative
            base_score = (
                mood_alignment * 0.35
                + genre_coherence * 0.25
                + energy_flow * 0.10
                + content_similarity * 0.15
                + session_similarity * 0.10
                + quality * 0.05
            )

            collaborative_score = 0.0
            if collaborative_ready and seeds:
                collaborative_score = self._collaborative_score(
                    candidate.track_id, seeds
                )

            if collaborative_ready and collaborative_weight > 0:
                blended = (
                    1 - collaborative_weight
                ) * base_score + collaborative_weight * collaborative_score
            else:
                blended = base_score

            final_score = blended
            final_score *= candidate.feedback_multiplier
            final_score *= candidate.diversity_penalty
            final_score *= max(0.0, candidate.extra_weight)

            scored.append(
                ScoredCandidate(
                    track_id=candidate.track_id,
                    artist=candidate.artist,
                    title=candidate.title,
                    score=final_score,
                    breakdown={
                        "base": base_score,
                        "collaborative": collaborative_score,
                        "mood_alignment": mood_alignment,
                        "mood_distance_avg": (
                            mood_distance_avg if mood_distance_avg is not None else -1.0
                        ),
                        "mood_distance_liked": (
                            mood_distance_liked
                            if mood_distance_liked is not None
                            else -1.0
                        ),
                        "genre_coherence": genre_coherence,
                        "energy_flow": energy_flow,
                        "feedback_multiplier": candidate.feedback_multiplier,
                        "diversity_penalty": candidate.diversity_penalty,
                        "extra_weight": candidate.extra_weight,
                        "final": final_score,
                    },
                )
            )

        scored.sort(key=lambda item: item.score, reverse=True)
        if LOG.isEnabledFor(logging.DEBUG):
            top_entry = scored[0] if scored else None
            LOG.debug(
                "Recommender scoring summary: %s",
                {
                    "guild": str(guild_id),
                    "candidates": len(candidates),
                    "collaborative_ready": collaborative_ready,
                    "collaborative_weight": collaborative_weight,
                    "session_mood_vector": bool(session_vector),
                    "target_mood": target_mood,
                    "top_track": (
                        getattr(top_entry, "track_id", None) if top_entry else None
                    ),
                    "top_score": (
                        getattr(top_entry, "score", None) if top_entry else None
                    ),
                },
            )
        return scored

    def snapshot_settings(self) -> Dict[str, Any]:
        return {
            "toggle_overrides": dict(self._collab_toggles),
            "weight_overrides": dict(self._collab_weight_overrides),
            "default_weight": _DEFAULT_COLLAB_WEIGHT,
        }

    def _collaborative_score(self, track_id: str, seeds: Sequence[str]) -> float:
        if not track_id:
            return 0.0

        total = 0.0
        count = 0
        for seed in seeds:
            similarity = self._collaborative.track_similarity(track_id, seed)
            if similarity <= 0:
                continue
            total += similarity
            count += 1
        if not count:
            return 0.0
        return total / count

    @staticmethod
    def _mood_distance(
        session_vector: Sequence[float],
        candidate_vector: Sequence[float],
    ) -> float:
        length = min(len(session_vector), len(candidate_vector))
        if length == 0:
            return 0.0
        sum_sq = 0.0
        for idx in range(length):
            session_val = float(session_vector[idx])
            candidate_val = float(candidate_vector[idx])
            sum_sq += (session_val - candidate_val) ** 2
        max_distance = math.sqrt(float(length))
        if max_distance <= 0:
            return 0.0
        distance = math.sqrt(sum_sq) / max_distance
        return max(0.0, min(1.0, distance))

    @staticmethod
    def _score_genre_coherence(
        candidate_genres: List[str],
        session_genres_set: set,
    ) -> float:
        """
        Phase 3 Task 4.1: Genre coherence scoring.

        Calculate overlap between candidate genres and session focus genres.
        Returns score from 0.0 (no overlap) to 1.0 (perfect match).
        """
        if not candidate_genres or not session_genres_set:
            return 0.5  # Neutral score when no genre data available

        # Normalize candidate genres to lowercase
        candidate_set = set(g.lower() for g in candidate_genres)

        # Calculate overlap ratio
        overlap = len(candidate_set & session_genres_set)
        min_len = min(len(candidate_set), len(session_genres_set))

        if min_len == 0:
            return 0.5

        overlap_ratio = overlap / min_len
        return max(0.0, min(1.0, overlap_ratio))

    @staticmethod
    def _score_energy_flow(
        candidate_energy: Optional[float],
        last_energy: Optional[float],
        energy_trend: float,
    ) -> float:
        """
        Phase 3 Task 4.2: Energy flow scoring.

        Penalize sharp energy jumps, bonus for trend continuation.
        Returns score from 0.0 (terrible transition) to 1.0 (perfect flow).
        """
        if candidate_energy is None or last_energy is None:
            return 0.7  # Neutral-positive score when energy data unavailable

        # Calculate energy delta
        energy_delta = candidate_energy - last_energy

        # Start with perfect score
        score = 1.0

        # Penalize sharp jumps (>0.4 delta)
        if abs(energy_delta) > 0.4:
            score -= 0.5  # Major penalty for jarring transitions
        elif abs(energy_delta) > 0.3:
            score -= 0.2  # Minor penalty for large jumps

        # Bonus for continuing energy trend
        if abs(energy_trend) > 0.1:  # Only if there's a clear trend
            if (energy_trend > 0 and energy_delta > 0) or (
                energy_trend < 0 and energy_delta < 0
            ):
                score += 0.3  # Bonus for continuing the trend

        return max(0.0, min(1.0, score))

    @staticmethod
    def _clamp01(value: float) -> float:
        return max(0.0, min(1.0, value))

    @staticmethod
    def _guild_key(guild_id: int | str) -> str:
        return str(guild_id)


__all__ = [
    "ContextualRecommender",
    "CandidateFeatures",
    "ScoredCandidate",
]
