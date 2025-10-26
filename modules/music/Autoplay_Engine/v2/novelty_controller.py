"""
Novelty Controller for Autoplay V2 - Adaptive Exploration Manager

Manages dynamic exploration rate based on user engagement (skip patterns).
Implements controlled novelty injection to prevent genre leaps while enabling discovery.

Part of Issue #3 - Contextual Arc Recommender
"""

import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

LOG = logging.getLogger(__name__)


class ExplorationPhase(Enum):
    """Current exploration phase for the session"""
    STABLE = "stable"  # Low skip rate, stay in current cluster
    RISING_BOREDOM = "rising_boredom"  # Increasing skips, add variety
    HIGH_EXPLORATION = "high_exploration"  # Many skips, explore more
    END_SESSION = "end_session"  # Long session, inject rediscovery


@dataclass
class NoveltyConfig:
    """Configuration for novelty behavior"""
    # Exploration rate bounds
    min_exploration_rate: float = 0.05  # Minimum 5% novelty
    max_exploration_rate: float = 0.25  # Maximum 25% novelty
    base_exploration_rate: float = 0.10  # Default 10%
    
    # Exploration radius (embedding distance)
    max_distance: float = 0.8  # Don't pick candidates >0.8 distance away
    same_cluster_threshold: float = 0.25  # <0.25 = same cluster
    similar_cluster_threshold: float = 0.50  # 0.25-0.50 = similar
    mild_novelty_threshold: float = 0.80  # 0.50-0.80 = mild novelty
    
    # Skip rate thresholds for phase detection
    stable_skip_threshold: float = 0.15  # <15% skips = stable
    boredom_skip_threshold: float = 0.35  # >35% skips = bored
    
    # Rediscovery timing
    rediscovery_interval: int = 25  # Every N songs, try rediscovery
    
    # Candidate mixing proportions by phase
    phase_proportions: Dict[ExplorationPhase, Dict[str, float]] = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.phase_proportions:
            self.phase_proportions = {
                ExplorationPhase.STABLE: {
                    "core": 0.70,
                    "similar": 0.20,
                    "novel": 0.10,
                    "rediscover": 0.00,
                },
                ExplorationPhase.RISING_BOREDOM: {
                    "core": 0.55,
                    "similar": 0.30,
                    "novel": 0.15,
                    "rediscover": 0.00,
                },
                ExplorationPhase.HIGH_EXPLORATION: {
                    "core": 0.40,
                    "similar": 0.35,
                    "novel": 0.25,
                    "rediscover": 0.00,
                },
                ExplorationPhase.END_SESSION: {
                    "core": 0.60,
                    "similar": 0.20,
                    "novel": 0.10,
                    "rediscover": 0.10,
                },
            }


class NoveltyController:
    """
    Manages adaptive exploration rate and candidate group mixing.
    
    Implements the "Novelty Controller" component from Issue #3.
    Models behavior after Spotify/Apple Music's gradual novelty injection.
    """
    
    def __init__(
        self,
        *,
        config: Optional[NoveltyConfig] = None,
        verbose: int = 0,
    ):
        """
        Args:
            config: Novelty configuration (uses defaults if None)
            verbose: Verbosity level (0=off, 1=info, 2=debug)
        """
        self.config = config or NoveltyConfig()
        self._verbose = verbose
        
        LOG.info(
            "🎲 [NoveltyController] Initialized (base_rate=%.2f, max_distance=%.2f)",
            self.config.base_exploration_rate,
            self.config.max_distance,
        )
    
    def compute_exploration_rate(
        self,
        *,
        skip_rate: float,
        consecutive_skips: int,
    ) -> float:
        """
        Compute dynamic exploration rate based on skip patterns.
        
        Formula: novelty_rate = clamp(base + (skip_rate - 0.25) * 0.4, min, max)
        
        Args:
            skip_rate: Recent skip rate (0.0 to 1.0)
            consecutive_skips: Current skip streak
            
        Returns:
            Exploration rate (0.0 to 1.0)
        """
        # Base calculation
        base = self.config.base_exploration_rate
        rate = base + (skip_rate - 0.25) * 0.4
        
        # Apply consecutive skip boost
        if consecutive_skips >= 3:
            rate += 0.05  # Boost exploration after 3+ consecutive skips
        
        # Clamp to bounds
        rate = max(self.config.min_exploration_rate, rate)
        rate = min(self.config.max_exploration_rate, rate)
        
        if self._verbose >= 2:
            LOG.debug(
                "🎲 [Novelty] Exploration rate: %.2f (skip_rate=%.2f, streak=%d)",
                rate, skip_rate, consecutive_skips
            )
        
        return rate
    
    def detect_exploration_phase(
        self,
        *,
        skip_rate: float,
        songs_since_novelty: int,
        session_duration_minutes: float,
    ) -> ExplorationPhase:
        """
        Detect current exploration phase based on session state.
        
        Args:
            skip_rate: Recent skip rate
            songs_since_novelty: Songs played since last novelty pick
            session_duration_minutes: Session length in minutes
            
        Returns:
            Current ExplorationPhase
        """
        # Long session -> try rediscovery
        if songs_since_novelty >= self.config.rediscovery_interval:
            return ExplorationPhase.END_SESSION
        
        # High skip rate -> explore more
        if skip_rate > self.config.boredom_skip_threshold:
            return ExplorationPhase.HIGH_EXPLORATION
        
        # Rising skip rate -> add variety
        if skip_rate > self.config.stable_skip_threshold:
            return ExplorationPhase.RISING_BOREDOM
        
        # Low skip rate -> stay stable
        return ExplorationPhase.STABLE
    
    def get_candidate_proportions(
        self,
        phase: ExplorationPhase,
    ) -> dict:
        """
        Get candidate mixing proportions for the current phase.
        
        Args:
            phase: Current exploration phase
            
        Returns:
            Dict with keys: core, similar, novel, rediscover (proportions sum to 1.0)
        """
        proportions = self.config.phase_proportions[phase]
        
        if self._verbose >= 1:
            LOG.info(
                "🎲 [Novelty] Phase '%s': core=%.0f%% similar=%.0f%% novel=%.0f%% rediscover=%.0f%%",
                phase.value,
                proportions["core"] * 100,
                proportions["similar"] * 100,
                proportions["novel"] * 100,
                proportions["rediscover"] * 100,
            )
        
        return proportions
    
    def classify_candidate_by_distance(
        self,
        distance: float,
    ) -> str:
        """
        Classify a candidate as core/similar/novel based on embedding distance.
        
        Args:
            distance: Embedding distance from current context (0.0 to 1.0+)
            
        Returns:
            Category: "core", "similar", "novel", or "rejected"
        """
        if distance <= self.config.same_cluster_threshold:
            return "core"
        elif distance <= self.config.similar_cluster_threshold:
            return "similar"
        elif distance <= self.config.mild_novelty_threshold:
            return "novel"
        else:
            return "rejected"  # Too far, reject
    
    def compute_cluster_score(
        self,
        distance: float,
    ) -> float:
        """
        Compute cluster alignment score from embedding distance.
        
        Returns weighted bonus/penalty:
        - 0-0.25: +0.4 (same cluster)
        - 0.25-0.5: +0.2 (similar cluster)
        - 0.5-0.8: +0.05 (mild novelty)
        - >0.8: -0.1 (hard novelty, usually rejected)
        
        Args:
            distance: Embedding distance (0.0 to 1.0+)
            
        Returns:
            Cluster score adjustment (-0.1 to +0.4)
        """
        if distance <= 0.25:
            return 0.4  # Same cluster - strong bonus
        elif distance <= 0.5:
            return 0.2  # Similar cluster - moderate bonus
        elif distance <= 0.8:
            return 0.05  # Mild novelty - small bonus
        else:
            return -0.1  # Hard novelty - penalty
    
    def should_inject_rediscovery(
        self,
        songs_since_novelty: int,
    ) -> bool:
        """
        Check if it's time to inject a rediscovery pick.
        
        Args:
            songs_since_novelty: Counter for songs since last novelty
            
        Returns:
            True if rediscovery should be attempted
        """
        return songs_since_novelty >= self.config.rediscovery_interval
    
    def filter_by_distance(
        self,
        candidates: List[Tuple[str, float]],
    ) -> List[Tuple[str, float]]:
        """
        Filter candidates by maximum distance threshold.
        
        Args:
            candidates: List of (candidate_id, distance) tuples
            
        Returns:
            Filtered list excluding candidates beyond max_distance
        """
        filtered = [
            (cid, dist)
            for cid, dist in candidates
            if dist <= self.config.max_distance
        ]
        
        rejected_count = len(candidates) - len(filtered)
        if rejected_count > 0 and self._verbose >= 1:
            LOG.info(
                "🎲 [Novelty] Filtered %d candidates (distance >%.2f)",
                rejected_count, self.config.max_distance
            )
        
        return filtered
    
    def get_stats(self) -> dict:
        """Get configuration stats for observability."""
        return {
            "base_exploration_rate": self.config.base_exploration_rate,
            "max_distance": self.config.max_distance,
            "rediscovery_interval": self.config.rediscovery_interval,
            "phase_proportions": {
                phase.value: props
                for phase, props in self.config.phase_proportions.items()
            },
        }
