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
    """Current exploration phase for the session (Apple Music-inspired)"""
    STABLE = "stable"  # Low skip rate, short session, stay in current cluster
    PASSIVE_EXPLORATION = "passive_exploration"  # Long session, no skips, safe to diversify
    RISING_BOREDOM = "rising_boredom"  # Increasing skips, add variety
    HIGH_EXPLORATION = "high_exploration"  # Many skips, explore more
    REDISCOVERY = "rediscovery"  # Time to return to roots


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
    
    # Time-based phase detection (Apple Music pattern)
    passive_exploration_time: float = 15.0  # After 15 minutes, diversify even if no skips
    rediscovery_interval: int = 25  # Every N songs, try rediscovery
    rediscovery_time: float = 30.0  # After 30 minutes, return to roots
    
    # Apple Music-inspired 5-slot buffer proportions by phase
    phase_proportions: Dict[ExplorationPhase, Dict[str, float]] = field(default_factory=dict)
    
    def __post_init__(self):
        if not self.phase_proportions:
            # Apple Music-style 5-slot buffer:
            # Slot 1: Core (familiar, high replay)
            # Slot 2: Similar (same vibe, different artist)
            # Slot 3: Bridge (mild novelty, transitional)
            # Slot 4: Discovery (new vibe exploration)
            # Slot 5: Safe Harbor (return to roots)
            self.phase_proportions = {
                ExplorationPhase.STABLE: {
                    "core": 0.50,      # 50% - keep the flow going
                    "similar": 0.30,   # 30% - same vibe, variety
                    "bridge": 0.15,    # 15% - gentle transitions
                    "discovery": 0.05, # 5% - minimal exploration
                    "safe_harbor": 0.00,  # 0% - no rediscovery yet
                },
                ExplorationPhase.PASSIVE_EXPLORATION: {
                    "core": 0.30,      # 30% - reduce core picks
                    "similar": 0.25,   # 25% - maintain vibe
                    "bridge": 0.25,    # 25% - more transitions
                    "discovery": 0.20, # 20% - significant exploration
                    "safe_harbor": 0.00,  # 0% - no rediscovery yet
                },
                ExplorationPhase.RISING_BOREDOM: {
                    "core": 0.35,      # 35% - some familiarity
                    "similar": 0.25,   # 25% - maintain vibe
                    "bridge": 0.20,    # 20% - transitions
                    "discovery": 0.20, # 20% - exploration
                    "safe_harbor": 0.00,  # 0% - no rediscovery yet
                },
                ExplorationPhase.HIGH_EXPLORATION: {
                    "core": 0.25,      # 25% - minimal familiarity
                    "similar": 0.20,   # 20% - less vibe matching
                    "bridge": 0.25,    # 25% - many transitions
                    "discovery": 0.30, # 30% - heavy exploration
                    "safe_harbor": 0.00,  # 0% - no rediscovery yet
                },
                ExplorationPhase.REDISCOVERY: {
                    "core": 0.40,      # 40% - back to familiarity
                    "similar": 0.20,   # 20% - maintain vibe
                    "bridge": 0.10,    # 10% - less transitions
                    "discovery": 0.10, # 10% - minimal exploration
                    "safe_harbor": 0.20,  # 20% - RETURN TO ROOTS
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
        
        Implements Apple Music-style time-based diversification:
        - <15 min, no skips: STABLE (flow state, keep it going)
        - >15 min, no skips: PASSIVE_EXPLORATION (safe to diversify)
        - >30 min or N songs: REDISCOVERY (return to roots)
        - High skips: HIGH_EXPLORATION (user is searching)
        
        This solves the "30-minute diversification" observation where
        Apple Music gradually explores even with zero skips.
        
        Args:
            skip_rate: Recent skip rate (0.0-1.0)
            songs_since_novelty: Songs played since last novelty pick
            session_duration_minutes: Session length in minutes
            
        Returns:
            Current ExplorationPhase
        """
        # Priority 1: Rediscovery (time to return to roots)
        if (songs_since_novelty >= self.config.rediscovery_interval or 
            session_duration_minutes >= self.config.rediscovery_time):
            return ExplorationPhase.REDISCOVERY
        
        # Priority 2: High skip rate (user is actively searching)
        if skip_rate > self.config.boredom_skip_threshold:
            return ExplorationPhase.HIGH_EXPLORATION
        
        # Priority 3: Rising skip rate (add variety)
        if skip_rate > self.config.stable_skip_threshold:
            return ExplorationPhase.RISING_BOREDOM
        
        # Priority 4: Low skip rate - check time-based diversification
        # This is the KEY insight: "no skips" means different things at different times
        if skip_rate <= self.config.stable_skip_threshold:
            if session_duration_minutes >= self.config.passive_exploration_time:
                # PASSIVE_EXPLORATION: Long session, no skips = background listening
                # Safe to gradually diversify (user isn't actively engaged)
                return ExplorationPhase.PASSIVE_EXPLORATION
            else:
                # STABLE: Short session, no skips = flow state
                # Keep the vibe tight, user is actively enjoying
                return ExplorationPhase.STABLE
        
        # Fallback: stable
        return ExplorationPhase.STABLE
    
    def get_candidate_proportions(
        self,
        phase: ExplorationPhase,
    ) -> dict:
        """
        Get candidate mixing proportions for the current phase (Apple Music 5-slot buffer).
        
        Apple Music-style recommendation slots:
        1. **Core**: Familiar tracks (high replay, liked artists)
        2. **Similar**: Same vibe, different artists (maintain mood)
        3. **Bridge**: Mild novelty, transitional tracks (smooth exploration)
        4. **Discovery**: New vibe exploration (learning preferences)
        5. **Safe Harbor**: Return to roots (replayed favorites)
        
        Args:
            phase: Current exploration phase
            
        Returns:
            Dict with keys: core, similar, bridge, discovery, safe_harbor (proportions sum to 1.0)
        """
        proportions = self.config.phase_proportions[phase]
        
        if self._verbose >= 1:
            LOG.info(
                "🎲 [Novelty] Phase '%s': core=%.0f%% similar=%.0f%% bridge=%.0f%% discovery=%.0f%% safe_harbor=%.0f%%",
                phase.value,
                proportions["core"] * 100,
                proportions["similar"] * 100,
                proportions["bridge"] * 100,
                proportions["discovery"] * 100,
                proportions["safe_harbor"] * 100,
            )
        
        return proportions
    
    def classify_candidate_by_distance(
        self,
        distance: float,
        familiarity_score: float = 0.0,
    ) -> str:
        """
        Classify a candidate into Apple Music-style 5-slot buffer categories.
        
        Categories:
        - **core**: Very close (<0.25 distance) OR high familiarity (>0.7)
        - **similar**: Close (0.25-0.50 distance) with moderate familiarity
        - **bridge**: Mid-range (0.50-0.80 distance), transitional
        - **discovery**: Far (>0.80 distance), exploration
        - **safe_harbor**: High replay (familiarity >0.8), return to roots
        
        Args:
            distance: Embedding distance from current context (0.0 to 1.0+)
            familiarity_score: Track familiarity (0.0-1.0, based on replays/likes)
            
        Returns:
            Category: "core", "similar", "bridge", "discovery", "safe_harbor", or "rejected"
        """
        # Priority: Safe Harbor (highly familiar tracks)
        if familiarity_score > 0.8:
            return "safe_harbor"
        
        # Distance-based classification
        if distance <= self.config.same_cluster_threshold:
            return "core"
        elif distance <= self.config.similar_cluster_threshold:
            return "similar"
        elif distance <= self.config.mild_novelty_threshold:
            return "bridge"
        elif distance <= self.config.max_distance:
            return "discovery"
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
