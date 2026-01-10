"""Shared constants, enums, and configuration for V3 Autoplay Engine.

This module provides centralized configuration that all other V3 modules depend on.
"""
from __future__ import annotations

import os
import json
from enum import Enum, auto
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


# =============================================================================
# Environment Variable Helpers (supports multiple naming conventions)
# =============================================================================

def get_env_with_fallback(*names: str, default: str = "") -> str:
    """Get environment variable with fallback names.
    
    Supports both the reimplementation plan naming (GEMINI_API_KEYS) and
    legacy naming (GeminiApiKeys) for backwards compatibility.
    
    Args:
        *names: Variable names to try in order
        default: Default value if none found
        
    Returns:
        First found value or default
    """
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value
    return default


def get_env_int(*names: str, default: int = 0) -> int:
    """Get integer environment variable with fallback names."""
    value = get_env_with_fallback(*names, default=str(default))
    try:
        return int(value)
    except ValueError:
        return default


def get_env_float(*names: str, default: float = 0.0) -> float:
    """Get float environment variable with fallback names."""
    value = get_env_with_fallback(*names, default=str(default))
    try:
        return float(value)
    except ValueError:
        return default


def get_env_list(*names: str, default: Optional[List[str]] = None) -> List[str]:
    """Get comma-separated list environment variable with fallback names."""
    value = get_env_with_fallback(*names, default="")
    if not value:
        return default or []
    return [item.strip() for item in value.split(",") if item.strip()]


# =============================================================================
# Directory Configuration
# =============================================================================

# Base paths
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent  # Discord-TodolistGrabber/
CACHE_ROOT = PROJECT_ROOT / "cache" / "Autoplay" / "v3"

# Cache subdirectories
MAPPINGS_DIR = CACHE_ROOT / "mappings"
METADATA_DIR = CACHE_ROOT / "metadata"
SESSIONS_DIR = CACHE_ROOT / "sessions"
DAYDREAMER_DIR = CACHE_ROOT / "daydreamer"

# Ensure directories exist (do lazily on first access to avoid import-time side effects)
_directories_initialized = False

def ensure_cache_directories() -> None:
    """Ensure cache directories exist. Called lazily to avoid import-time IO."""
    global _directories_initialized
    if _directories_initialized:
        return
    for directory in [MAPPINGS_DIR, METADATA_DIR, SESSIONS_DIR, DAYDREAMER_DIR]:
        directory.mkdir(parents=True, exist_ok=True)
    _directories_initialized = True

# =============================================================================
# Versioning
# =============================================================================

# V3 Engine version for API compatibility
V3_ENGINE_VERSION = "3.0.0"

# Component versions - increment when schema changes require migration
CACHE_VERSION = 1       # Cache schema version
MAPPING_VERSION = 1     # Song mapping format version  
METADATA_VERSION = 1    # Song metadata format version
SESSION_VERSION = 1     # Session state format version

# For external API responses
def get_v3_version_info() -> dict:
    """Get version information for API responses and diagnostics."""
    return {
        "engine": V3_ENGINE_VERSION,
        "cache_schema": CACHE_VERSION,
        "mapping_schema": MAPPING_VERSION,
        "metadata_schema": METADATA_VERSION,
        "session_schema": SESSION_VERSION
    }

# =============================================================================
# Cache Configuration
# =============================================================================

SHARD_MAX_ENTRIES = 5000  # Maximum entries per shard file

# Shard naming: a, b, ..., z, aa, ab, ...
def get_next_shard_suffix(current: str) -> str:
    """Generate next shard suffix in sequence: a, b, ..., z, aa, ab, ..."""
    if not current:
        return "a"
    
    if current == "z":
        return "aa"
    
    if len(current) == 1:
        return chr(ord(current) + 1)
    
    # Multi-character suffix
    last_char = current[-1]
    if last_char == "z":
        # Increment previous character and reset last to 'a'
        return get_next_shard_suffix(current[:-1]) + "a"
    else:
        return current[:-1] + chr(ord(last_char) + 1)


# =============================================================================
# Session State Enums
# =============================================================================

class SessionState(Enum):
    """Session warmth states based on song count and behavior."""
    COLD = "cold"      # 1-10 songs: Unstable, guessing
    WARM = "warm"      # 11-25 songs: Stable, consistent
    HOT = "hot"        # 25+ songs: Diversifying, extended session
    EXTENDED = "extended"  # 50+ songs: Long-running session
    
    @staticmethod
    def from_song_count(song_count: int, consecutive_skips: int = 0) -> 'SessionState':
        """Determine session state from song count and skip patterns.
        
        Args:
            song_count: Number of songs played in session
            consecutive_skips: Number of consecutive skips (3+ cycles back to COLD)
        
        Returns:
            The appropriate SessionState
        """
        # Apple Music style: cycle back to COLD on excessive skips
        if consecutive_skips >= 3:
            return SessionState.COLD
        
        if song_count <= 10:
            return SessionState.COLD
        elif song_count <= 25:
            return SessionState.WARM
        elif song_count <= 50:
            return SessionState.HOT
        else:
            return SessionState.EXTENDED


class CacheType(Enum):
    """Types of caches managed by the system."""
    MAPPINGS = "mappings"       # Deezer/YouTube/Last.fm ID mappings
    METADATA = "metadata"       # Song analysis metadata
    ANALYSIS = "analysis"       # Raw analysis data
    PREFERENCES = "preferences" # User preference data
    SESSIONS = "sessions"       # Session history data


class AnalysisPriority(Enum):
    """Priority levels for song analysis queue.
    
    Lower values = higher priority. Used for priority queue ordering.
    """
    IMMEDIATE = 0  # Currently playing or about to play
    HIGH = 1       # In buffer (next 5 songs)
    MEDIUM = 2     # In extended queue
    LOW = 3        # Background/daydreamer exploration
    BATCH = 4      # Bulk analysis during idle


class AnalysisMode(Enum):
    """Analysis depth modes."""
    QUICK = "quick"           # Fast analysis, minimum features
    STANDARD = "standard"     # Normal analysis, balanced
    DEEP = "deep"             # Full analysis, all features
    FULL = "full"             # Complete analysis (all layers)
    PHYSICS_ONLY = "physics"  # Only physics/audio features


class SkipType(Enum):
    """Skip classification based on play percentage (Apple Music style)."""
    IMMEDIATE = "immediate"  # <10% played - strong dislike
    EARLY = "early"          # <30% played - dislike
    MID = "mid"              # <70% played - mild dislike  
    LATE = "late"            # <90% played - neutral
    COMPLETED = "completed"  # 90%+ played - positive


class RecommendationSource(Enum):
    """Sources for recommendation candidates."""
    LASTFM_SIMILAR = "lastfm_similar"  # Last.fm similar tracks
    COLLABORATIVE = "collaborative"     # Collaborative filtering
    GEMINI = "gemini"                   # Gemini AI suggestions
    GENRE_BASED = "genre_based"         # Genre/mood matching
    DAYDREAM = "daydream"               # Background exploration


class EventType(Enum):
    """Event types for pub/sub communication."""
    # Song lifecycle events
    SONG_STARTED = auto()
    SONG_ENDED = auto()
    SONG_ANALYZED = auto()
    SONG_ANALYSIS_FAILED = auto()
    
    # Mapping events
    MAPPING_COMPLETE = auto()
    MAPPING_FAILED = auto()
    
    # Buffer events
    BUFFER_UPDATED = auto()
    BUFFER_CLEARED = auto()
    BUFFER_SONG_READY = auto()
    BUFFER_LOW = auto()
    BUFFER_REFILLED = auto()
    
    # Session events
    SESSION_STARTED = auto()
    SESSION_ENDED = auto()
    SESSION_STATE_CHANGED = auto()
    
    # Playback events
    SONG_PLAYED = auto()
    SONG_SKIPPED = auto()
    USER_REQUEST = auto()
    
    # Analysis events
    ANALYSIS_REQUESTED = auto()
    ANALYSIS_COMPLETED = auto()
    
    # Recommender events
    RECOMMENDATION_READY = auto()
    CANDIDATES_NEEDED = auto()
    
    # Daydreamer events
    DAYDREAM_STARTED = auto()
    DAYDREAM_BATCH_COMPLETE = auto()
    DAYDREAM_PAUSED = auto()
    DAYDREAM_RESUMED = auto()
    
    # System events
    RATE_LIMIT_HIT = auto()
    CACHE_LOADED = auto()
    CACHE_UPDATED = auto()
    
    # User feedback events
    USER_FEEDBACK = auto()
    
    # Context events
    CONTEXT_SHIFT = auto()


# =============================================================================
# Skip Weight Configuration 
# =============================================================================

@dataclass
class SkipWeightConfig:
    """Configuration for skip weight calculation."""
    # Thresholds (percentage of song played)
    strong_dislike_threshold: float = 0.10   # <10% = strong dislike
    dislike_threshold: float = 0.30          # <30% = dislike genre/mood
    mild_dislike_threshold: float = 0.70     # <70% = mild dislike
    neutral_threshold: float = 0.90          # <90% = neutral
    
    # Weights (negative values, 0 = neutral/positive)
    strong_dislike_weight: float = -1.0
    dislike_weight: float = -0.7
    mild_dislike_weight: float = -0.4
    neutral_weight: float = -0.1
    positive_weight: float = 0.0
    
    def calculate_weight(self, play_percentage: float) -> float:
        """Calculate skip weight based on percentage of song played."""
        if play_percentage < self.strong_dislike_threshold:
            return self.strong_dislike_weight
        elif play_percentage < self.dislike_threshold:
            return self.dislike_weight
        elif play_percentage < self.mild_dislike_threshold:
            return self.mild_dislike_weight
        elif play_percentage < self.neutral_threshold:
            return self.neutral_weight
        else:
            return self.positive_weight


SKIP_WEIGHT_CONFIG = SkipWeightConfig()


# =============================================================================
# Gemini API Configuration
# =============================================================================

@dataclass
class GeminiConfig:
    """Configuration for Gemini API usage."""
    primary_model: str = "gemini-2.5-flash"
    lite_model: str = "gemini-2.5-flash-lite"
    
    # Rate limits (conservative free tier estimates)
    rpm: int = 15           # Requests per minute
    tpm: int = 32000        # Tokens per minute
    rpd: int = 1500         # Requests per day
    
    # Batching configuration - BULK PROCESSING TO AVOID API SPAM
    batch_timeout_seconds: float = 5.0   # Max wait before sending batch
    max_batch_size: int = 10             # Max songs per bulk request (reduced for safety)
    min_batch_size: int = 3              # Wait for at least this many before sending
    batch_aggregation_window: float = 3.0  # Seconds to aggregate songs before processing
    
    # Retry configuration
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 300.0   # 5 minutes
    backoff_multiplier: float = 2.0
    max_retries: int = 5
    
    # Grounding priority (use grounding for these tasks)
    grounding_priority: List[str] = field(default_factory=lambda: [
        "explicit_content",
        "cultural_vibe",
        "canonical_title"
    ])
    
    @property
    def is_free_tier(self) -> bool:
        """Check if using free tier from environment."""
        return os.getenv("GEMINI_FREE_TIER", "true").lower() == "true"
    
    @property
    def api_keys(self) -> List[str]:
        """Get Gemini API keys from environment.
        
        Supports both naming conventions:
        - New (reimplementation plan): GEMINI_API_KEYS
        - Legacy: GeminiApiKeys
        
        Keys can be comma-separated for rotation.
        """
        return get_env_list("GEMINI_API_KEYS", "GeminiApiKeys", default=[])
    
    @property
    def is_available(self) -> bool:
        """Check if Gemini API is available (has at least one key)."""
        return len(self.api_keys) > 0


GEMINI_CONFIG = GeminiConfig()


# =============================================================================
# Song Analyzer Configuration (Worker Pools)
# =============================================================================

@dataclass
class AnalyzerConfig:
    """
    Configuration for song analysis workers.
    
    EfficientAT and Librosa analysis runs via worker pools to avoid
    blocking the event loop. Gemini analysis is batched to avoid
    API spam.
    """
    # Worker pool configuration
    analysis_worker_count: int = 3  # Default 3 workers for EfficientAT/Librosa
    max_concurrent_downloads: int = 3  # Parallel preview downloads
    max_concurrent_analysis: int = 3   # Parallel audio analysis
    
    # Queue configuration
    max_queue_size: int = 1000  # Maximum songs in analysis queue
    priority_queue_enabled: bool = True
    
    # Gemini bulk processing
    gemini_batch_enabled: bool = True
    gemini_batch_size: int = 10        # Songs per bulk Gemini request
    gemini_batch_timeout: float = 5.0  # Max wait before flushing batch
    
    # Audio settings
    preview_max_size_mb: float = 5.0   # Max preview file size
    sample_rate: int = 22050           # Audio sample rate for Librosa
    hop_length: int = 512              # Librosa hop length
    
    # Analysis timeout
    analysis_timeout_seconds: float = 60.0  # Per-song analysis timeout
    
    @classmethod
    def from_env(cls) -> 'AnalyzerConfig':
        """Load configuration from environment variables."""
        return cls(
            analysis_worker_count=int(os.getenv("AUTOPLAY_ANALYSIS_WORKERS", "3")),
            max_concurrent_downloads=int(os.getenv("AUTOPLAY_MAX_DOWNLOADS", "3")),
            gemini_batch_size=int(os.getenv("AUTOPLAY_GEMINI_BATCH_SIZE", "10"))
        )


ANALYZER_CONFIG = AnalyzerConfig.from_env()


# =============================================================================
# Session Configuration
# =============================================================================

@dataclass
class SessionConfig:
    """Configuration for session management."""
    max_concurrent_sessions: int = 2
    consecutive_request_limit: int = 5   # User requests before session ends (-1 to disable)
    buffer_size: int = 5                 # Fixed, Apple Music style
    
    # State thresholds
    cold_max_songs: int = 10
    warm_max_songs: int = 25
    # hot = 25+ songs
    
    @classmethod
    def from_env(cls) -> 'SessionConfig':
        """Load configuration from environment variables.
        
        Supports both naming conventions:
        - New (reimplementation plan): MAX_CONCURRENT_SESSIONS
        - Legacy: AUTOPLAY_MAX_SESSIONS
        """
        return cls(
            max_concurrent_sessions=get_env_int(
                "MAX_CONCURRENT_SESSIONS", "AUTOPLAY_MAX_SESSIONS", default=2
            ),
            consecutive_request_limit=get_env_int(
                "AUTOPLAY_CONSECUTIVE_LIMIT", default=5
            )
        )


SESSION_CONFIG = SessionConfig.from_env()


# =============================================================================
# Daydreamer Configuration
# =============================================================================

@dataclass
class DaydreamerConfig:
    """Configuration for background exploration."""
    explore_mode_threshold: int = 5000      # Switch to maintenance mode above this
    explore_batch_size: int = 100           # Songs per batch in explore mode
    explore_interval_minutes: int = 30      # Interval between batches
    maintenance_batch_size: int = 50        # Songs per batch in maintenance mode
    maintenance_interval_minutes: int = 60  # Interval between batches
    
    # New releases
    new_releases_check_day: int = 0        # 0 = track from first run date
    max_new_release_songs: int = 500
    
    # Retry configuration
    genre_retry_attempts: int = 3
    
    # Deezer endpoints for new releases
    chart_endpoint: str = "chart/0/tracks"
    # editorial/{genre_id}/releases used dynamically


DAYDREAMER_CONFIG = DaydreamerConfig()


# =============================================================================
# Collaborative Filtering Configuration
# =============================================================================

@dataclass  
class CollaborativeConfig:
    """Configuration for collaborative filtering."""
    activation_threshold: int = 500  # Minimum fully analyzed songs required
    
    # Similarity weights for each layer
    physics_weight: float = 0.3
    semantics_weight: float = 0.5
    librarian_weight: float = 0.2
    
    # Vector dimensions
    timbre_dimensions: int = 13      # MFCC dimensions from Librosa
    embedding_dimensions: int = 527  # EfficientAT output dimensions


COLLABORATIVE_CONFIG = CollaborativeConfig()


# =============================================================================
# Mapping Configuration
# =============================================================================

@dataclass
class MappingConfig:
    """Configuration for Deezer/YouTube/Last.fm mappings."""
    fuzzy_match_threshold: float = 0.65  # 65% match required
    max_retries: int = 5
    
    # Last.fm configuration
    lastfm_similar_limit: int = 100     # Max similar tracks per request
    lastfm_rate_limit_rps: float = 5.0  # Max requests per second
    
    # Deezer search
    deezer_search_limit: int = 25       # Max results per search


MAPPING_CONFIG = MappingConfig()


# =============================================================================
# Webhook Configuration
# =============================================================================

@dataclass
class WebhookConfig:
    """Configuration for error reporting webhooks."""
    url: Optional[str] = None
    timeout_seconds: float = 10.0
    
    @classmethod
    def from_env(cls) -> 'WebhookConfig':
        """Load webhook URL from environment."""
        return cls(url=os.getenv("WEBHOOK_URL"))
    
    @property
    def is_enabled(self) -> bool:
        return self.url is not None and self.url.strip() != ""


WEBHOOK_CONFIG = WebhookConfig.from_env()


# =============================================================================
# Metadata Schema Types
# =============================================================================

@dataclass
class PhysicsLayer:
    """Physics layer metadata (Librosa analysis).
    
    Extracted features:
    - Tempo (BPM) and musical key/mode
    - Loudness (dB) and energy levels
    - Spectral characteristics (centroid, rolloff)
    - Timbre representation (MFCC coefficients)
    - Percussiveness indicator (zero crossing rate)
    """
    bpm: float
    key: str
    mode: str  # "major" or "minor"
    loudness_db: float
    energy: float  # Normalized RMS energy (0-1)
    spectral_centroid: float
    spectral_rolloff: float
    mfcc_coefficients: Optional[List[float]] = None  # 13 MFCC coefficients for timbre
    zero_crossing_rate: Optional[float] = None  # Percussiveness indicator


@dataclass
class SemanticsLayer:
    """Semantics layer metadata (EfficientAT analysis).
    
    Fields:
    - embedding: Neural audio embedding vector (128-dim from EfficientAT)
    - instrument_tags: List of detected instruments
    - sound_tags: List of detected sound characteristics  
    - predicted_genres: Optional genre predictions from audio
    - quality_score: Audio quality assessment (0-1)
    """
    embedding: List[float]  # Neural embedding vector
    instrument_tags: List[str]  # Detected instruments
    sound_tags: List[str]  # Sound characteristics
    predicted_genres: Optional[List[str]] = None  # Genre predictions from audio
    quality_score: Optional[float] = None  # Audio quality (0-1)


@dataclass
class LibrarianLayer:
    """Librarian layer metadata (Gemini analysis).
    
    Also aliased as 'librarian_info' for compatibility.
    
    Contains contextual/cultural metadata about the song:
    - Genre classification
    - Mood/theme tagging
    - Energy/danceability metrics
    - Cultural context
    - Content flags
    """
    # Core genre/mood classification
    genres: List[str] = field(default_factory=list)
    moods: List[str] = field(default_factory=list)
    themes: List[str] = field(default_factory=list)
    
    # Energy and danceability (0-1 scale)
    energy_level: Optional[float] = None
    danceability: Optional[float] = None
    
    # Cultural context
    cultural_vibe: List[str] = field(default_factory=list)
    
    # Content flags
    explicit_content: bool = False
    
    # Canonical metadata (Gemini-corrected)
    canonical_title: Optional[str] = None
    canonical_artist: Optional[str] = None
    
    # Additional context
    release_era: Optional[str] = None  # e.g., "2020s", "1980s"
    micro_genre: List[str] = field(default_factory=list)  # Fine-grained genre tags


@dataclass
class SongMetadata:
    """Complete song metadata with all layers.
    
    Fields:
    - song_id: Primary identifier (Deezer ID preferred)
    - title, artist, album: Song info
    - audio_features: PhysicsLayer (Librosa analysis)
    - semantic_features: SemanticsLayer (EfficientAT analysis)
    - librarian_info: LibrarianLayer (Gemini analysis)
    """
    song_id: str = ""
    
    # Basic song info
    title: str = ""
    artist: str = ""
    album: Optional[str] = None
    duration_ms: Optional[int] = None
    preview_url: Optional[str] = None
    isrc: Optional[str] = None
    
    # Analysis layers
    audio_features: Optional[PhysicsLayer] = None
    semantic_features: Optional[SemanticsLayer] = None
    librarian_info: Optional[LibrarianLayer] = None
    
    # Version tracking
    metadata_version: int = CACHE_VERSION
    analysis_version: int = 1
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    
    @property
    def is_fully_analyzed(self) -> bool:
        """Check if all three layers are complete."""
        return all([self.audio_features, self.semantic_features, self.librarian_info])
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "song_id": self.song_id,
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "duration_ms": self.duration_ms,
            "preview_url": self.preview_url,
            "isrc": self.isrc,
            "metadata_version": self.metadata_version,
            "analysis_version": self.analysis_version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        
        if self.audio_features:
            result["audio_features"] = {
                "bpm": self.audio_features.bpm,
                "key": self.audio_features.key,
                "mode": self.audio_features.mode,
                "loudness_db": self.audio_features.loudness_db,
                "energy": self.audio_features.energy,
                "spectral_centroid": self.audio_features.spectral_centroid,
                "spectral_rolloff": self.audio_features.spectral_rolloff,
                "mfcc_coefficients": self.audio_features.mfcc_coefficients,
                "zero_crossing_rate": self.audio_features.zero_crossing_rate,
            }
        
        if self.semantic_features:
            result["semantic_features"] = {
                "embedding": self.semantic_features.embedding,
                "instrument_tags": self.semantic_features.instrument_tags,
                "sound_tags": self.semantic_features.sound_tags,
                "predicted_genres": self.semantic_features.predicted_genres,
                "quality_score": self.semantic_features.quality_score,
            }
        
        if self.librarian_info:
            result["librarian_info"] = {
                "genres": self.librarian_info.genres,
                "moods": self.librarian_info.moods,
                "themes": self.librarian_info.themes,
                "energy_level": self.librarian_info.energy_level,
                "danceability": self.librarian_info.danceability,
                "cultural_vibe": self.librarian_info.cultural_vibe,
                "explicit_content": self.librarian_info.explicit_content,
                "canonical_title": self.librarian_info.canonical_title,
                "canonical_artist": self.librarian_info.canonical_artist,
                "release_era": self.librarian_info.release_era,
                "micro_genre": self.librarian_info.micro_genre,
            }
        
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SongMetadata':
        """Create from dictionary (JSON deserialization)."""
        audio_features = None
        semantic_features = None
        librarian_info = None
        
        if data.get("audio_features"):
            af = data["audio_features"]
            audio_features = PhysicsLayer(
                bpm=af.get("bpm", 0.0),
                key=af.get("key", "C"),
                mode=af.get("mode", "major"),
                loudness_db=af.get("loudness_db", 0.0),
                energy=af.get("energy", 0.5),
                spectral_centroid=af.get("spectral_centroid", 0.0),
                spectral_rolloff=af.get("spectral_rolloff", 0.0),
                mfcc_coefficients=af.get("mfcc_coefficients"),
                zero_crossing_rate=af.get("zero_crossing_rate")
            )
        
        if data.get("semantic_features"):
            sf = data["semantic_features"]
            semantic_features = SemanticsLayer(
                embedding=sf.get("embedding", []),
                instrument_tags=sf.get("instrument_tags", []),
                sound_tags=sf.get("sound_tags", []),
                predicted_genres=sf.get("predicted_genres"),
                quality_score=sf.get("quality_score")
            )
        
        if data.get("librarian_info"):
            li = data["librarian_info"]
            librarian_info = LibrarianLayer(
                genres=li.get("genres", []),
                moods=li.get("moods", []),
                themes=li.get("themes", []),
                energy_level=li.get("energy_level"),
                danceability=li.get("danceability"),
                cultural_vibe=li.get("cultural_vibe", []),
                explicit_content=li.get("explicit_content", False),
                canonical_title=li.get("canonical_title"),
                canonical_artist=li.get("canonical_artist"),
                release_era=li.get("release_era"),
                micro_genre=li.get("micro_genre", [])
            )
        
        return cls(
            song_id=data.get("song_id", ""),
            title=data.get("title", ""),
            artist=data.get("artist", ""),
            album=data.get("album"),
            duration_ms=data.get("duration_ms"),
            preview_url=data.get("preview_url"),
            isrc=data.get("isrc"),
            audio_features=audio_features,
            semantic_features=semantic_features,
            librarian_info=librarian_info,
            metadata_version=data.get("metadata_version", CACHE_VERSION),
            analysis_version=data.get("analysis_version", 1),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at")
        )


@dataclass
class SongMapping:
    """Mapping between Deezer, YouTube, and Last.fm identifiers."""
    deezer_id: str
    youtube_id: Optional[str] = None
    lastfm_id: Optional[str] = None  # Format: "artist+track"
    preview_url: Optional[str] = None
    parsed_title: Optional[str] = None
    parsed_artist: Optional[str] = None
    mapping_version: int = CACHE_VERSION
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "deezer_id": self.deezer_id,
            "youtube_id": self.youtube_id,
            "lastfm_id": self.lastfm_id,
            "preview_url": self.preview_url,
            "parsed_title": self.parsed_title,
            "parsed_artist": self.parsed_artist,
            "mapping_version": self.mapping_version
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SongMapping':
        """Create from dictionary (JSON deserialization)."""
        return cls(
            deezer_id=data["deezer_id"],
            youtube_id=data.get("youtube_id"),
            lastfm_id=data.get("lastfm_id"),
            preview_url=data.get("preview_url"),
            parsed_title=data.get("parsed_title"),
            parsed_artist=data.get("parsed_artist"),
            mapping_version=data.get("mapping_version", CACHE_VERSION)
        )


# =============================================================================
# Logging Configuration
# =============================================================================

def get_verbosity() -> int:
    """Get verbosity level from environment.
    
    Levels:
        0 = Errors only (ERROR level)
        1 = Overview (WARNING level - errors + important events)
        2 = Detailed (INFO level - full operation logging)
        3 = Debug (DEBUG level - developer diagnostics)
    """
    return int(os.getenv("AUTOPLAY_V3_VERBOSITY", "1"))


VERBOSITY = get_verbosity()


def configure_v3_logging() -> None:
    """Configure logging levels based on AUTOPLAY_V3_VERBOSITY.
    
    This should be called once at engine startup to set appropriate
    log levels for all V3 modules.
    """
    import logging
    
    # Map verbosity to log level
    level_map = {
        0: logging.ERROR,    # Errors only
        1: logging.WARNING,  # Overview (errors + important events)  
        2: logging.INFO,     # Detailed
        3: logging.DEBUG     # Debug
    }
    
    log_level = level_map.get(VERBOSITY, logging.INFO)
    
    # Get the V3 package logger (parent of all module loggers)
    v3_logger = logging.getLogger("modules.music.Autoplay_Engine.v3")
    v3_logger.setLevel(log_level)
    
    # If no handlers exist, add a console handler
    if not v3_logger.handlers and not v3_logger.parent.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(log_level)
        formatter = logging.Formatter(
            '[V3-Autoplay] %(levelname)s | %(name)s | %(message)s'
        )
        handler.setFormatter(formatter)
        v3_logger.addHandler(handler)
    
    # Log the configured level
    if VERBOSITY >= 2:
        v3_logger.info(f"V3 Autoplay logging configured: verbosity={VERBOSITY}, level={logging.getLevelName(log_level)}")


# =============================================================================
# Config Classes (for tests and external usage)
# =============================================================================

@dataclass
class SessionThresholds:
    """Thresholds for session state transitions."""
    cold_max: int = 10    # Cold ends at 10 songs
    warm_max: int = 25    # Warm ends at 25 songs
    hot_min: int = 25     # Hot starts at 25 songs
    extended_min: int = 50  # Extended starts at 50 songs


@dataclass
class BufferConfig:
    """Buffer configuration."""
    size: int = 5   # Fixed 5-song buffer per Apple Music style
    min_fill_threshold: int = 2   # Refill when buffer drops below this
    max_refill_attempts: int = 3
    low_threshold: int = 2  # Trigger refill below this count
    prefetch_enabled: bool = True  # Enable prefetching


@dataclass
class CacheConfig:
    """Cache configuration."""
    base_path: str = str(CACHE_ROOT)
    max_entries_per_shard: int = SHARD_MAX_ENTRIES
    version: int = CACHE_VERSION


@dataclass
class AnalysisConfig:
    """Analysis configuration wrapper."""
    worker_count: int = ANALYZER_CONFIG.analysis_worker_count
    max_concurrent_downloads: int = ANALYZER_CONFIG.max_concurrent_downloads
    timeout_seconds: float = ANALYZER_CONFIG.analysis_timeout_seconds
    librosa_enabled: bool = True
    efficientat_model: str = "mn10_as"
    gemini_model: str = GEMINI_CONFIG.primary_model  # gemini-2.5-flash


@dataclass
class RateLimitConfig:
    """Rate limiting configuration."""
    gemini_rpm: int = GEMINI_CONFIG.rpm
    gemini_tpm: int = GEMINI_CONFIG.tpm
    lastfm_rps: float = MAPPING_CONFIG.lastfm_rate_limit_rps
    deezer_search_limit: int = MAPPING_CONFIG.deezer_search_limit
    deezer_requests_per_minute: int = 60  # Conservative estimate for Deezer
    lastfm_requests_per_minute: int = int(MAPPING_CONFIG.lastfm_rate_limit_rps * 60)
    gemini_batch_size: int = 50  # Per spec: 50 songs per batch


@dataclass
class V3Config:
    """
    Master configuration class for V3 Autoplay Engine.
    
    Aggregates all sub-configurations into a single object that can
    be passed to components during initialization.
    """
    thresholds: SessionThresholds = field(default_factory=SessionThresholds)
    buffer: BufferConfig = field(default_factory=BufferConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    
    # Top-level config values
    max_concurrent_sessions: int = SESSION_CONFIG.max_concurrent_sessions
    daydream_songs_per_cycle: int = 50
    collaborative_activation_threshold: int = COLLABORATIVE_CONFIG.activation_threshold
    
    @classmethod
    def from_env(cls) -> 'V3Config':
        """Create configuration from environment variables."""
        return cls(
            thresholds=SessionThresholds(),
            buffer=BufferConfig(),
            cache=CacheConfig(),
            analysis=AnalysisConfig(),
            rate_limits=RateLimitConfig(),
            max_concurrent_sessions=int(os.getenv("AUTOPLAY_MAX_SESSIONS", "2")),
            daydream_songs_per_cycle=int(os.getenv("AUTOPLAY_DAYDREAM_BATCH", "50"))
        )
