"""
Contextual Recommender - V3 Dual-Mode Architecture

V3 ARCHITECTURE (ML + Non-ML Support):
==========================================

ML MODE (High Accuracy):
    - Vibe: 512D-2048D learned embedding (EfficientAT MobileNet)
    - Distance: Cosine similarity on embeddings
    - Dependencies: librosa + torch + torchaudio
  - Accuracy: 85-95%

NON-ML MODE (Lightweight):
  - Vibe: 5D simple vibe (Librosa features only)
    1. energy (0.0-1.0): RMS loudness + dynamic variance
    2. valence (0.0-1.0): Mode (major/minor) + spectral brightness
    3. danceability (0.0-1.0): Tempo proximity to 120 BPM + beat strength
    4. acousticness (0.0-1.0): Spectral rolloff + zero-crossing rate (inverse)
    5. brightness (0.0-1.0): Spectral centroid (high-frequency content)
  - Distance: Euclidean distance on 5D vector
  - Dependencies: librosa only
  - Accuracy: 65-75%

FLOW VECTOR (4D) - Always computed for both modes:
  1. loudness (dB): e.g., -5.883 dB
  2. tempo (BPM): e.g., 120.0
  3. key (0-11): C=0, C#=1, ..., B=11
  4. mode (0/1): 0=minor, 1=major

Scoring Jobs:
1. Vibe Steering: 70% PULL to liked_mood_vector, 30% PUSH from disliked_mood_vector
2. Energy Flow: Penalize jarring dB jumps (>10dB = -0.6 penalty)
3. Harmonic Mixing: Camelot wheel compatibility for key transitions
"""
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np

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
    
    # V3 Architecture: Dual-Mode Support (ML + Non-ML)
    computed_embedding: Optional[Sequence[float]] = None  # EfficientAT embedding
    computed_embedding_model: Optional[str] = None  # "mn10_as"
    computed_embedding_dim: Optional[int] = None  # Actual dimension
    
    # Non-ML Mode: Simplified 5D vibe (Librosa-only)
    computed_simple_vibe: Optional[Sequence[float]] = None  # [energy, valence, danceability, acousticness, brightness]
    
    # Phase 3: Enhanced scoring fields
    genres: Optional[List[str]] = None  # For genre coherence scoring
    
    # FLOW VECTOR (4D): For DJ-quality transitions (always computed)
    computed_loudness: Optional[float] = None  # Loudness in dB (e.g., -5.883)
    computed_tempo: Optional[float] = None  # BPM as float
    computed_key: Optional[int] = None  # 0-11 (C=0, C#=1, ..., B=11)
    computed_mode: Optional[int] = None  # 0=minor, 1=major


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
        disliked_mood_vector: Optional[Sequence[float]] = None,  # NEW: For vibe steering
        energy_trend: float = 0.0,
        last_energy: Optional[float] = None,
        # Dual Vector Architecture: Flow features for harmonic mixing
        last_loudness: Optional[float] = None,
        last_tempo: Optional[float] = None,
        last_key: Optional[int] = None,
        last_mode: Optional[int] = None,
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
        disliked_vector = list(disliked_mood_vector) if disliked_mood_vector else None
        focus_genres_set = set(g.lower() for g in (session_focus_genres or []))

        scored: List[ScoredCandidate] = []
        for candidate in candidates:
            content_similarity = self._clamp01(candidate.content_similarity)
            session_similarity = self._clamp01(candidate.session_similarity)
            novelty = self._clamp01(candidate.novelty)
            quality = self._clamp01(candidate.quality)

            # V3 Architecture: Dual-Mode Vibe Similarity Scoring
            # Priority: computed_embedding (ML) > computed_simple_vibe (Non-ML) > mood_vector (V2 legacy)
            # 70% PULL towards liked tracks, 30% PUSH away from disliked tracks
            mood_alignment = 0.5
            liked_similarity = 0.5
            disliked_similarity = 0.5
            mood_distance_avg = None
            mood_distance_liked = None
            mood_distance_disliked = None
            
            candidate_vibe = None
            use_ml_mode = False
            use_nonml_mode = False
            
            if candidate.computed_embedding and len(candidate.computed_embedding) > 0:
                candidate_vibe = candidate.computed_embedding
                use_ml_mode = True
            elif candidate.computed_simple_vibe and len(candidate.computed_simple_vibe) == 5:
                candidate_vibe = candidate.computed_simple_vibe
                use_nonml_mode = True

            if session_vector and candidate_vibe:
                if use_ml_mode:
                    similarity_score = self._embedding_similarity(session_vector, candidate_vibe)
                    mood_distance_avg = 1.0 - similarity_score
                elif use_nonml_mode:
                    similarity_score = self._simple_vibe_similarity(session_vector, candidate_vibe)
                    mood_distance_avg = 1.0 - similarity_score
            elif session_vector:
                mood_alignment = 0.5

            if session_vector and candidate_vibe and liked_vector:
                if use_ml_mode:
                    liked_similarity = self._embedding_similarity(liked_vector, candidate_vibe)
                    mood_distance_liked = 1.0 - liked_similarity
                elif use_nonml_mode:
                    liked_similarity = self._simple_vibe_similarity(liked_vector, candidate_vibe)
                    mood_distance_liked = 1.0 - liked_similarity

            if session_vector and candidate_vibe and disliked_vector:
                if use_ml_mode:
                    disliked_similarity_score = self._embedding_similarity(disliked_vector, candidate_vibe)
                    disliked_similarity = disliked_similarity_score
                    mood_distance_disliked = 1.0 - disliked_similarity_score
                elif use_nonml_mode:
                    disliked_similarity_score = self._simple_vibe_similarity(disliked_vector, candidate_vibe)
                    disliked_similarity = disliked_similarity_score
                    mood_distance_disliked = 1.0 - disliked_similarity_score

            if mood_distance_liked is not None and mood_distance_disliked is not None:
                mood_alignment = (0.7 * liked_similarity) - (0.3 * disliked_similarity)
                mood_alignment = (mood_alignment + 0.3) / 1.0
                mood_alignment = self._clamp01(mood_alignment)
            elif mood_distance_liked is not None:
                mood_alignment = self._clamp01(liked_similarity)
            elif mood_distance_avg is not None:
                mood_alignment = self._clamp01(1.0 - mood_distance_avg)

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

            # Phase 3 Task 4.2: Energy flow scoring with loudness-based transitions
            energy_flow = self._score_energy_flow(
                candidate_energy=candidate.energy,
                last_energy=last_energy,
                energy_trend=energy_trend,
                candidate_loudness=candidate.computed_loudness,
                last_loudness=last_loudness,
            )

            # Dual Vector Architecture: Harmonic mixing for DJ-smooth transitions
            harmonic_coherence = self._score_harmonic_coherence(
                candidate_key=candidate.computed_key,
                candidate_mode=candidate.computed_mode,
                last_key=last_key,
                last_mode=last_mode,
            )

            # Updated score composition with harmonic mixing
            # 30% mood (vibe steering) + 20% genre + 15% harmonic + 10% energy flow + 25% content/session/quality
            base_score = (
                mood_alignment * 0.30  # Vibe steering with push/pull
                + genre_coherence * 0.20  # Genre coherence
                + harmonic_coherence * 0.15  # Key compatibility (NEW!)
                + energy_flow * 0.10  # Volume/energy transitions
                + content_similarity * 0.12
                + session_similarity * 0.08
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
                        "mood_distance_disliked": (
                            mood_distance_disliked
                            if mood_distance_disliked is not None
                            else -1.0
                        ),
                        "genre_coherence": genre_coherence,
                        "energy_flow": energy_flow,
                        "harmonic_coherence": harmonic_coherence,
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
        """
        DEPRECATED: Legacy Euclidean distance scorer for V2 compatibility.
        Use _embedding_similarity() for ML mode or _simple_vibe_similarity() for Non-ML mode.
        """
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
    def _embedding_similarity(
        session_embedding: Sequence[float],
        candidate_embedding: Sequence[float],
    ) -> float:
        """
        V3 ML Mode: Cosine similarity scorer for learned embeddings (512D-2048D).
        
        Cosine similarity measures the angle between two vectors, capturing semantic
        similarity better than Euclidean distance for high-dimensional embeddings.
        
        Args:
            session_embedding: Session's average embedding (512D-2048D)
            candidate_embedding: Candidate track's embedding (512D-2048D)
            
        Returns:
            Similarity score from 0.0 (completely different) to 1.0 (identical vibe)
            
        Formula:
            cosine_sim = (A · B) / (||A|| × ||B||)
            scaled = (cosine_sim + 1.0) / 2.0  # Scale from [-1,1] to [0,1]
        """
        if not session_embedding or not candidate_embedding:
            return 0.5  # Neutral score when data unavailable
        
        # Convert to numpy arrays
        v1 = np.array(session_embedding, dtype=np.float32)
        v2 = np.array(candidate_embedding, dtype=np.float32)
        
        # Check dimension compatibility
        if v1.shape[0] != v2.shape[0]:
            LOG.warning(
                f"⚠️ Embedding dimension mismatch: session={v1.shape[0]}D, candidate={v2.shape[0]}D"
            )
            return 0.5
        
        # Compute cosine similarity
        dot_product = np.dot(v1, v2)
        norm_v1 = np.linalg.norm(v1)
        norm_v2 = np.linalg.norm(v2)
        
        if norm_v1 == 0 or norm_v2 == 0:
            return 0.5  # Zero vector edge case
        
        cosine_sim = dot_product / (norm_v1 * norm_v2)
        
        # Scale from [-1, 1] to [0, 1]
        # -1 = opposite vibe, 0 = orthogonal, 1 = same vibe
        similarity = (cosine_sim + 1.0) / 2.0
        
        return max(0.0, min(1.0, similarity))

    @staticmethod
    def _simple_vibe_similarity(
        session_vibe: Sequence[float],
        candidate_vibe: Sequence[float],
    ) -> float:
        """
        V3 Non-ML Mode: Euclidean distance scorer for 5D simple vibe.
        
        Uses normalized Euclidean distance on Librosa-derived features:
        [energy, valence, danceability, acousticness, brightness]
        
        Args:
            session_vibe: Session's average 5D simple vibe
            candidate_vibe: Candidate track's 5D simple vibe
            
        Returns:
            Similarity score from 0.0 (very different) to 1.0 (very similar)
            
        Formula:
            distance = sqrt(sum((a[i] - b[i])^2)) / sqrt(5)  # Normalized
            similarity = 1.0 - distance
        """
        if not session_vibe or not candidate_vibe:
            return 0.5  # Neutral score when data unavailable
        
        length = min(len(session_vibe), len(candidate_vibe))
        if length == 0:
            return 0.5
        
        # Compute Euclidean distance
        sum_sq = 0.0
        for idx in range(length):
            session_val = float(session_vibe[idx])
            candidate_val = float(candidate_vibe[idx])
            sum_sq += (session_val - candidate_val) ** 2
        
        # Normalize by maximum possible distance
        max_distance = math.sqrt(float(length))
        if max_distance <= 0:
            return 0.5
        
        distance = math.sqrt(sum_sq) / max_distance
        
        # Convert distance to similarity (1.0 = same, 0.0 = opposite)
        similarity = 1.0 - distance
        
        return max(0.0, min(1.0, similarity))

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
        candidate_loudness: Optional[float] = None,
        last_loudness: Optional[float] = None,
    ) -> float:
        """
        Phase 3 Task 4.2: Energy flow scoring with loudness-based transitions.

        Uses actual loudness (dB) when available for precise transition scoring.
        Penalize sharp volume jumps, bonus for trend continuation.
        Returns score from 0.0 (terrible transition) to 1.0 (perfect flow).
        
        Args:
            candidate_energy: Deprecated 0-1 normalized energy (fallback)
            last_energy: Deprecated 0-1 normalized energy (fallback)
            energy_trend: Energy trend from last 3 tracks (-1.0 to 1.0)
            candidate_loudness: Loudness in dB (e.g., -5.883)
            last_loudness: Previous track loudness in dB
        """
        # Prefer loudness-based scoring (more accurate)
        if candidate_loudness is not None and last_loudness is not None:
            # Calculate dB delta
            loudness_delta_db = abs(candidate_loudness - last_loudness)
            
            # Start with perfect score
            score = 1.0
            
            # Penalize jarring volume jumps
            # Thresholds: <3dB = imperceptible, 3-6dB = noticeable, 6-10dB = significant, >10dB = jarring
            if loudness_delta_db > 10.0:
                score -= 0.6  # Severe penalty for jarring jumps
            elif loudness_delta_db > 6.0:
                score -= 0.3  # Moderate penalty for significant jumps
            elif loudness_delta_db > 3.0:
                score -= 0.1  # Minor penalty for noticeable changes
            
            # Bonus for continuing volume trend (if energy_trend indicates direction)
            if abs(energy_trend) > 0.1:
                delta_direction = candidate_loudness - last_loudness
                if (energy_trend > 0 and delta_direction > 0) or (energy_trend < 0 and delta_direction < 0):
                    score += 0.2  # Bonus for continuing trend
            
            return max(0.0, min(1.0, score))
        
        # Fallback: Use deprecated energy (0-1 normalized) if loudness unavailable
        if candidate_energy is None or last_energy is None:
            return 0.7  # Neutral-positive score when data unavailable

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
    def _score_harmonic_coherence(
        candidate_key: Optional[int],
        candidate_mode: Optional[int],
        last_key: Optional[int],
        last_mode: Optional[int],
    ) -> float:
        """
        Phase 3: Harmonic mixing scorer using Camelot wheel logic.
        
        Scores key compatibility for smooth DJ-style transitions.
        Based on Circle of Fifths and relative major/minor relationships.
        
        Args:
            candidate_key: 0-11 (C=0, C#=1, ..., B=11)
            candidate_mode: 0=minor, 1=major
            last_key: Previous track's key (0-11)
            last_mode: Previous track's mode (0=minor, 1=major)
            
        Returns:
            Score from 0.0 (incompatible/clashing) to 1.0 (perfect match)
        """
        if candidate_key is None or last_key is None:
            return 0.6  # Neutral score when key data unavailable
        
        if candidate_mode is None or last_mode is None:
            return 0.6
        
        # Perfect match: same key and mode
        if candidate_key == last_key and candidate_mode == last_mode:
            return 1.0
        
        # Compatible transitions (Camelot wheel logic):
        # 1. Same key, different mode (relative major/minor) - Very compatible
        if candidate_key == last_key and candidate_mode != last_mode:
            return 0.95
        
        # 2. +1 semitone (same mode) - Circle of fifths, very smooth
        if (candidate_key == (last_key + 1) % 12) and candidate_mode == last_mode:
            return 0.90
        
        # 3. -1 semitone (same mode) - Reverse circle of fifths, smooth
        if (candidate_key == (last_key - 1) % 12) and candidate_mode == last_mode:
            return 0.85
        
        # 4. +7 semitones / Perfect fifth (same mode) - Musically related
        if (candidate_key == (last_key + 7) % 12) and candidate_mode == last_mode:
            return 0.80
        
        # 5. -7 semitones / Perfect fourth (same mode) - Musically related
        if (candidate_key == (last_key - 7) % 12) and candidate_mode == last_mode:
            return 0.75
        
        # 6. Minor to relative major (+3 semitones) or vice versa
        if last_mode == 0 and candidate_mode == 1:  # Minor -> Major
            if candidate_key == (last_key + 3) % 12:
                return 0.85
        elif last_mode == 1 and candidate_mode == 0:  # Major -> Minor
            if candidate_key == (last_key - 3) % 12:
                return 0.85
        
        # 7. Tritone (6 semitones) - Dissonant/clashing, low score
        if abs(candidate_key - last_key) == 6:
            return 0.2
        
        # 8. Other intervals - Moderate compatibility
        # Calculate semitone distance (minimum of clockwise/counter-clockwise)
        semitone_delta = min(
            abs(candidate_key - last_key),
            12 - abs(candidate_key - last_key)
        )
        
        # Closer keys = better compatibility
        if semitone_delta <= 2:
            return 0.70
        elif semitone_delta <= 4:
            return 0.50
        else:
            return 0.30

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
