# V3 Autoplay Engine - Implementation Plan

> **Status**: All clarifications resolved ✅
> **Created**: January 4, 2026
> **Ready for Implementation**: Yes

---

## 📋 Final Decisions Summary

### All Clarifications Resolved

| # | Question | Final Decision |
|---|----------|----------------|
| 1 | Metadata structure | **A) Nested** - `physics`, `semantics`, `librarian` layers |
| 2 | Mapping vs Metadata storage | **B) Separate files** - `mappings/` and `metadata/` directories |
| 3 | Grounding usage strategy | **B) Prioritize** - Use for `explicit_content`, `cultural_vibe`, `canonical_title` |
| 4 | Session crash recovery | **Remove previous sessions** - Start fresh after crash |
| 5 | Daydreamer new releases | **Combination** - `chart/0/tracks` + `editorial/{genre_id}/releases` |
| 6 | Cold/Warm/Hot thresholds | **1-10 Cold, 11-25 Warm, 25+ Hot/Extended** (can cycle back to Cold on skip patterns) |
| 7 | Collaborative filtering trigger | **500 fully analyzed songs** (with all 3 metadata layers) |
| 8 | Webhook event types | **Sufficient for now** - `mapping_failure` only |
| 9 | .env variables | **Complete** - No additions needed |
| 10 | Test mocking strategy | **Both** - Unit tests mocked, integration tests use real APIs |

---

## 🔧 Technical Specifications

### Gemini API Configuration

Based on web research (ai.google.dev):

| Setting | Value |
|---------|-------|
| Primary Model | `gemini-2.5-flash` |
| Lite Model | `gemini-2.5-flash-lite` |
| Rate Limits (Free Tier) | Per-project, varies by model |
| Grounding | Billed per search query (Gemini 3) or per prompt (Gemini 2.5) |
| Grounding Support | ✅ All Gemini 2.5 models support Google Search grounding |

### Rate Limiting Strategy

```python
# Gemini Manager Rate Limiting
RATE_LIMITS = {
    "free_tier": {
        "rpm": 15,              # Requests per minute (conservative estimate)
        "tpm": 32000,           # Tokens per minute
        "rpd": 1500,            # Requests per day (conservative)
        "grounding_priority": [  # Prioritized uses for grounding
            "explicit_content",
            "cultural_vibe", 
            "canonical_title"
        ]
    }
}
```

### Directory Structure

```
./cache/Autoplay/v3/
├── mappings/
│   ├── mappings_a.json      # Deezer ↔ YouTube ↔ Last.fm IDs
│   ├── mappings_b.json
│   └── ...
├── metadata/
│   ├── metadata_a.json      # Physics + Semantics + Librarian layers
│   ├── metadata_b.json
│   └── ...
├── sessions/
│   └── (cleared on startup) # Session recovery files
├── daydreamer/
│   ├── state.json           # Current batch progress
│   ├── genre_distribution.json
│   └── last_run.json
└── version.json             # Cache version for migration
```

### Metadata Schema (Nested - Final)

```json
{
  "deezer_id": "12345",
  "physics": {
    "computed_bpm": 128.5,
    "computed_key": "C Major",
    "computed_loudness": -6.2,
    "timbre_vector": [0.1, 0.2, 0.3, ...]
  },
  "semantics": {
    "embedding_vector": [0.5, 0.3, ...],
    "instrument_tags": {"guitar": 0.9, "synth": 0.1, "drums": 0.95},
    "quality_score": 0.95
  },
  "librarian": {
    "canonical_title": "Song Name",
    "canonical_artist": "Artist Name",
    "release_era": "2020s",
    "cultural_vibe": ["Gym", "Party", "Driving"],
    "micro_genre": ["Future Bass", "EDM"],
    "explicit_content": false
  },
  "metadata_version": 1
}
```

### Mapping Schema

```json
{
  "deezer_id": "12345",
  "youtube_id": "dQw4w9WgXcQ",
  "lastfm_id": "artist+track",
  "preview_url": "https://cdns-preview-d.dzcdn.net/...",
  "parsed_title": "Song Name",
  "parsed_artist": "Artist Name",
  "mapping_version": 1
}
```

### Session State Thresholds

```python
class SessionState:
    COLD = "cold"      # 1-10 songs: Unstable, guessing
    WARM = "warm"      # 11-25 songs: Stable, consistent
    HOT = "hot"        # 25+ songs: Diversifying, extended session
    
    @staticmethod
    def get_state(song_count: int, consecutive_skips: int) -> str:
        # If user keeps skipping, cycle back to COLD
        if consecutive_skips >= 3:
            return SessionState.COLD
        
        if song_count <= 10:
            return SessionState.COLD
        elif song_count <= 25:
            return SessionState.WARM
        else:
            return SessionState.HOT
```

---

## 📦 Implementation Phases

### Phase 1: Foundation (Core Infrastructure)

**Estimated Time**: 2-3 days
**Dependencies**: None

#### 1.1 Cache Manager

**File**: `modules/music/Autoplay_Engine/v3/cache_manager.py`

**Features**:

- Sharded JSON storage (5000+ entries per shard)
- Automatic shard creation (`_a`, `_b`, ... `_z`, `_aa`, `_ab`, ...)
- Thread-safe read/write with file locking
- Crash recovery with atomic writes (write to `.tmp`, then rename)
- Version migration support
- Separate directories for `mappings/`, `metadata/`, `sessions/`, `daydreamer/`

**Tests**: `tests/test_v3_cache_manager.py`

```python
# Test cases:
- test_create_shard_on_threshold
- test_search_across_shards
- test_atomic_write_crash_recovery
- test_corrupt_entry_repair
- test_version_migration
- test_concurrent_access
```

#### 1.2 Event Bus (Pub/Sub)

**File**: `modules/music/Autoplay_Engine/v3/event_bus.py`

**Features**:

- Async event-driven communication
- Event types: `SONG_ANALYZED`, `MAPPING_COMPLETE`, `BUFFER_UPDATED`, `SESSION_STATE_CHANGED`, etc.
- Priority queue for urgent events
- Event logging for debugging

**Tests**: `tests/test_v3_event_bus.py`

```python
# Test cases:
- test_publish_subscribe
- test_priority_ordering
- test_async_handlers
- test_error_isolation
```

#### 1.3 Gemini Manager

**File**: `modules/music/Autoplay_Engine/v3/gemini_manager.py`

**Features**:

- API key rotation from `GeminiApiKeys=["key1", "key2"]`
- Request batching (wait for count OR timeout, whichever first)
- Self-rate-limiting before hitting API limits
- Exponential backoff on errors
- Grounding prioritization (only for `explicit_content`, `cultural_vibe`, `canonical_title`)
- Model selection (`gemini-2.5-flash` vs `gemini-2.5-flash-lite`)
- Greenlight system for batch completion

**Tests**: `tests/test_v3_gemini_manager.py`

```python
# Test cases (mocked):
- test_api_key_rotation
- test_batch_aggregation
- test_rate_limiting
- test_exponential_backoff
- test_grounding_prioritization
- test_greenlight_callback
```

---

### Phase 2: Mappings & Analysis

**Estimated Time**: 3-4 days
**Dependencies**: Phase 1

#### 2.1 Mappings Module

**File**: `modules/music/Autoplay_Engine/v3/mappings.py`

**Features**:

- Deezer ↔ YouTube ↔ Last.fm bidirectional mapping
- YouTube title parsing via Gemini (remove "Official Video", "Lyrics", etc.)
- Deezer search with fuzzy matching (65% threshold)
- Fallback: Gemini grounding → simplified search
- 5 retry attempts before giving up
- Webhook reporting for failures (if `WEBHOOK_URL` set)
- Lavalink integration for YouTube search (only when playing)

**Tests**: `tests/test_v3_mappings.py`

```python
# Test cases:
- test_youtube_to_deezer_mapping
- test_lastfm_bulk_mapping
- test_fuzzy_match_threshold
- test_retry_with_grounding_fallback
- test_webhook_failure_report
- test_lavalink_youtube_search
```

#### 2.2 Song Analyzer

**File**: `modules/music/Autoplay_Engine/v3/song_analyzer.py`

**Features**:

- Priority queue: IMMEDIATE (playing), HIGH (buffer), MEDIUM (queue), LOW (daydreamer)
- **Worker Pool (ThreadPoolExecutor)**: Configurable worker count for CPU-bound analysis
  - Default 3 workers (set via `AUTOPLAY_ANALYSIS_WORKERS` env var)
  - Runs Librosa and EfficientAT analysis in parallel
  - Configured via `ANALYZER_CONFIG` in constants.py
- **Librosa analysis** (via worker pool): BPM, key, loudness, timbre (MFCC)
- **EfficientAT analysis** (via worker pool): embeddings, instruments, quality
- **Gemini analysis (BULK PROCESSING)**: 
  - Queues songs for batch processing to avoid API spam
  - Batch size configurable (default 10 songs per request)
  - Batch worker aggregates requests with timeout
  - IMMEDIATE priority songs get inline Gemini calls
  - Other priorities go through batch queue
- Download 30s Deezer preview → analyze → delete
- Skip already-analyzed songs
- Trigger recommender on completion
- State persistence for crash recovery

**Configuration** (in `constants.py`):
```python
ANALYZER_CONFIG = AnalyzerConfig(
    analysis_worker_count=3,        # Workers for EfficientAT/Librosa
    max_concurrent_downloads=3,     # Parallel preview downloads
    gemini_batch_size=10,           # Songs per bulk Gemini request
    gemini_batch_timeout=5.0,       # Seconds before flushing batch
)
```

**Tests**: `tests/test_v3_song_analyzer.py`

```python
# Test cases:
- test_priority_queue_ordering
- test_worker_pool_concurrency
- test_librosa_feature_extraction
- test_efficientat_embedding (mocked)
- test_gemini_batch_aggregation
- test_gemini_bulk_processing
- test_skip_already_analyzed
- test_crash_recovery_continue
```

#### 2.3 Update Dependency Manager

**File**: `modules/music/Autoplay_Engine/v3/dependency_manager.py` (existing)

**Updates**:

- Ensure Librosa dependencies (numpy, scipy, etc.)
- Ensure EfficientAT model download
- GPU detection and fallback
- Add health check method

---

### Phase 3: Intelligence Layer

**Estimated Time**: 4-5 days
**Dependencies**: Phase 2

#### 3.1 Context Analyzer

**File**: `modules/music/Autoplay_Engine/v3/context_analyzer.py`

**Features**:

- Track play history per session
- Skip weight calculation (Apple Music style):
  - <10% played: -1.0 (strong dislike)
  - <30% played: -0.7 (dislike genre/mood)
  - <70% played: -0.4 (mild dislike)
  - <90% played: -0.1 (neutral)
  - ≥90% played: 0.0 (positive)
- Consecutive skip pattern analysis:
  - 1 skip: Note preference
  - 2 skips: Avoid artist temporarily
  - 3+ skips: Switch genre/mood → cycle back to COLD state
- Time-weighted song preference (recent songs weighted higher)
- Build session profile (preferred BPM range, energy, genres)
- Vote handling for multi-user sessions (simple percentage)

**Tests**: `tests/test_v3_context_analyzer.py`

```python
# Test cases:
- test_skip_weight_calculation
- test_consecutive_skip_patterns
- test_time_weighted_preferences
- test_profile_building
- test_multi_user_vote_weighting
- test_state_transition_on_skips
```

#### 3.2 Novelty Controller

**File**: `modules/music/Autoplay_Engine/v3/novelty_controller.py`

**Features**:

- Nudge recommender toward diversity
- Prevent genre stagnation
- Balance familiar vs new artists
- Escape room logic: find right genre without straying
- Communicate with recommender bidirectionally

**Tests**: `tests/test_v3_novelty_controller.py`

```python
# Test cases:
- test_diversity_nudge
- test_genre_stagnation_prevention
- test_artist_familiarity_balance
```

#### 3.3 Vector Search Index (formerly Collaborative Filtering)

**File**: `modules/music/Autoplay_Engine/v3/vector_search_index.py`

**Purpose**: Content-based audio similarity using audio features (NOT user behavior).

**Features**:

- Activate only when 500+ fully analyzed songs in cache
- Load all metadata for bulk vector operations
- Calculate AUDIO CONTENT similarity using:
  - Physics layer: BPM, key, loudness, timbre (Librosa)
  - Semantics layer: EfficientAT embedding vectors
  - Librarian layer: genre, mood bag-of-words
- Return ranked list of similar songs by audio characteristics
- Lazy loading with LRU cache for performance

**Tests**: `tests/test_v3_vector_search_index.py`

```python
# Test cases:
- test_activation_threshold
- test_vector_similarity_calculation
- test_multi_layer_similarity
- test_performance_with_5000_songs
```

#### 3.4 Collaborative Recommender (NEW)

**File**: `modules/music/Autoplay_Engine/v3/collaborative_recommender.py`

**Purpose**: TRUE collaborative filtering based on user behavior patterns.

**Features**:

- **Transition Matrix**: Records "what plays after what" across sessions
  - Tracks play-through rate, explicit likes, skip counts per transition
  - Provides transition quality scores for recommendations
- **User Behavior Profiles**: Per-user preference tracking
  - Liked songs (from "More Like This" button)
  - Skip behavior (song_id -> skip count)
  - Completed songs (90%+ play-through)
  - Preferred/avoided artists
  - Genre affinity scores
- **Group Consensus**: Multi-user VC handling
  - Weighted voting based on activity
  - Veto songs (heavily skipped by multiple users)
  - Safe picks (liked by multiple users)
- **Behavioral Boost**: Score multipliers based on user engagement signals

**Tests**: `tests/test_v3_collaborative_recommender.py`

```python
# Test cases:
- test_transition_matrix_recording
- test_transition_score_calculation
- test_user_profile_updates
- test_group_consensus_voting
- test_behavioral_boost_calculation
- test_persistence_and_loading
```

#### 3.5 Recommender Core

**File**: `modules/music/Autoplay_Engine/v3/recommender.py`

**Features**:

- Head chef orchestration
- Integrate: Context Analyzer, Novelty Controller, Collaborative Filtering
- Cold/Warm/Hot state machine
- Request more candidates if below threshold
- Apple Music style slow burn transitions
- Communicate with Buffer Manager for output

**Tests**: `tests/test_v3_recommender.py`

```python
# Test cases:
- test_cold_state_recommendations
- test_warm_state_stability
- test_hot_state_diversification
- test_state_transition_on_skips
- test_threshold_candidate_request
- test_integration_with_subsystems
```

---

### Phase 4: Session Management

**Estimated Time**: 2-3 days
**Dependencies**: Phase 3

#### 4.1 Session Manager

**File**: `modules/music/Autoplay_Engine/v3/session_manager.py`

**Features**:

- Maximum 2 concurrent sessions (from `AUTOPLAY_MAX_SESSIONS`)
- Session reservation per guild
- Session end triggers:
  - A) Bot leaves voice channel
  - B) User requests 5+ songs consecutively (from `AUTOPLAY_CONSECUTIVE_LIMIT`)
  - C) User disables in settings
- Clear all previous sessions on startup (crash recovery = fresh start)
- Persist session data to disk during operation
- Enable mid-session → "Active next session" message

**Tests**: `tests/test_v3_session_manager.py`

```python
# Test cases:
- test_concurrent_session_limit
- test_session_reservation
- test_session_end_on_bot_leave
- test_session_end_on_consecutive_requests
- test_startup_session_cleanup
- test_mid_session_enable_handling
```

#### 4.2 Buffer Manager

**File**: `modules/music/Autoplay_Engine/v3/buffer_manager.py`

**Features**:

- 5 song buffer (non-configurable, Apple Music style)
- Communicate with Recommender for next songs
- Communicate with Novelty Controller for diversity
- Clear buffer immediately on user request
- Trigger context reanalysis after buffer clear + skip
- Map to YouTube via Lavalink when ready to play

**Apple Music-Style "Testing the Waters" Buffer**:
- Buffer contains mix of SAFE and EXPLORATORY slots
- **SAFE picks**: High confidence based on proven preferences, good transitions
- **EXPLORATORY picks**: "Testing the waters" to discover new music
- Slot composition adapts to session state:
  - COLD (1-10 songs): 4 safe, 1 exploratory (cautious)
  - WARM (11-25 songs): 3 safe, 2 exploratory (balanced)
  - HOT (25+ songs): 2 safe, 3 exploratory (adventurous)
- Tracks exploratory success rate and adapts:
  - If success rate <30%: shift more to safe picks
  - If success rate >70%: increase exploratory picks

**Tests**: `tests/test_v3_buffer_manager.py`

```python
# Test cases:
- test_buffer_fill_to_5
- test_slot_type_composition_cold
- test_slot_type_composition_warm
- test_slot_type_composition_hot
- test_exploratory_success_tracking
- test_buffer_clear_on_user_request
- test_context_reanalysis_trigger
- test_youtube_mapping_on_play
```

#### 4.3 Daydreamer

**File**: `modules/music/Autoplay_Engine/v3/daydreamer.py`

**Features**:

- Run only when no active sessions
- Priority 3 (lowest)
- <5000 songs: Explore mode (100 songs/30 min)
- ≥5000 songs: Maintenance mode (50 songs/hour)
- New releases: Once per month (track first run date)
  - Source: `chart/0/tracks` + `editorial/{genre_id}/releases`
- Genre distribution balancing
- Shift notes for continuity
- Pause on session activation
- State persistence for crash recovery

**Tests**: `tests/test_v3_daydreamer.py`

```python
# Test cases:
- test_no_run_with_active_session
- test_explore_mode_rate
- test_maintenance_mode_rate
- test_monthly_new_releases
- test_genre_distribution_balance
- test_pause_on_session_start
- test_state_persistence
```

---

### Phase 5: Integration & Testing

**Estimated Time**: 3-4 days
**Dependencies**: Phase 4

#### 5.1 Integration Module

**File**: `modules/music/Autoplay_Engine/v3/__init__.py`

**Features**:

- Initialize all modules
- Wire up event bus connections
- Startup sequence:
  1. Clear previous sessions
  2. Load cache manager
  3. Check/download dependencies
  4. Initialize Gemini manager
  5. Start daydreamer if no sessions
- Shutdown sequence with state persistence

#### 5.2 Discord Integration

**File**: Update `commands/Music/play.py` and related

**Features**:

- Hook autoplay trigger on queue end
- Session manager integration
- Buffer playback
- Settings menu integration

#### 5.3 Integration Tests

**File**: `tests/test_v3_integration.py`

```python
# Full workflow tests (use real APIs with rate limiting):
- test_cold_start_to_recommendation
- test_skip_pattern_genre_switch
- test_buffer_refill_cycle
- test_multi_session_handling
- test_daydreamer_background_fetch
- test_crash_recovery_workflow
```

#### 5.4 Test Fixtures

**Directory**: `tests/fixtures/`

```
fixtures/
├── sample_songs.json           # 100 mock songs with all metadata
├── mock_deezer_responses.json  # Deezer API response mocks
├── mock_lastfm_responses.json  # Last.fm API response mocks
├── mock_gemini_responses.json  # Gemini API response mocks
└── sample_session.json         # Sample session state
```

---

## 🧪 Testing Strategy

### Unit Tests (Mocked)

- Fast execution
- No network dependencies
- Run on every commit
- `pytest tests/test_v3_*.py -v --ignore=tests/test_v3_integration.py`

### Integration Tests (Real APIs)

- Rate-limited execution
- Requires API keys in `.env`
- Run separately with `-m integration` marker
- `pytest tests/test_v3_integration.py -v -m integration`

### Test Coverage Requirements

- Minimum 80% coverage for all modules
- 100% coverage for Cache Manager and Session Manager (critical paths)

---

## 📊 Progress Tracking

| Phase | Module | Status | Tests |
|-------|--------|--------|-------|
| 1.1 | Cache Manager | ⬜ Not Started | ⬜ |
| 1.2 | Event Bus | ⬜ Not Started | ⬜ |
| 1.3 | Gemini Manager | ⬜ Not Started | ⬜ |
| 2.1 | Mappings | ⬜ Not Started | ⬜ |
| 2.2 | Song Analyzer | ⬜ Not Started | ⬜ |
| 2.3 | Dependency Manager Update | ⬜ Not Started | ⬜ |
| 3.1 | Context Analyzer | ⬜ Not Started | ⬜ |
| 3.2 | Novelty Controller | ⬜ Not Started | ⬜ |
| 3.3 | Vector Search Index | ⬜ Not Started | ⬜ |
| 3.4 | Collaborative Recommender | ⬜ Not Started | ⬜ |
| 3.5 | Recommender Core | ⬜ Not Started | ⬜ |
| 4.1 | Session Manager | ⬜ Not Started | ⬜ |
| 4.2 | Buffer Manager | ⬜ Not Started | ⬜ |
| 4.3 | Daydreamer | ⬜ Not Started | ⬜ |
| 5.1 | Integration Module | ⬜ Not Started | ⬜ |
| 5.2 | Discord Integration | ⬜ Not Started | ⬜ |
| 5.3 | Integration Tests | ⬜ Not Started | ⬜ |

---

## 📁 File Summary

### New Files to Create

```
modules/music/Autoplay_Engine/v3/
├── __init__.py              # Integration & initialization
├── cache_manager.py         # Sharded cache with recovery
├── event_bus.py             # Pub/sub communication
├── gemini_manager.py        # API key rotation & batching
├── mappings.py              # Deezer ↔ YouTube ↔ Last.fm
├── song_analyzer.py         # Librosa + EfficientAT + Gemini
├── context_analyzer.py      # Skip detection & profiles
├── novelty_controller.py    # Diversity nudging
├── vector_search_index.py   # Content-based audio similarity (EfficientAT + Librosa)
├── collaborative_recommender.py # User behavior-based recommendations (Transition Matrix)
├── recommender.py           # Head chef orchestration
├── session_manager.py       # Concurrent session limits
├── buffer_manager.py        # 5-song Apple Music-style buffer with slot types
├── daydreamer.py            # Background exploration
└── constants.py             # Shared constants, enums & config (ANALYZER_CONFIG)

tests/
├── test_v3_cache_manager.py
├── test_v3_event_bus.py
├── test_v3_gemini_manager.py
├── test_v3_mappings.py
├── test_v3_song_analyzer.py
├── test_v3_context_analyzer.py
├── test_v3_novelty_controller.py
├── test_v3_vector_search_index.py
├── test_v3_collaborative_recommender.py
├── test_v3_recommender.py
├── test_v3_session_manager.py
├── test_v3_buffer_manager.py
├── test_v3_daydreamer.py
├── test_v3_integration.py
└── fixtures/
    ├── sample_songs.json
    ├── mock_deezer_responses.json
    ├── mock_lastfm_responses.json
    └── mock_gemini_responses.json
```

### Files to Update

```
modules/music/Autoplay_Engine/v3/dependency_manager.py  # Add health check
modules/enviromentfilegenerator.py                       # Add new .env vars
commands/Music/play.py                                   # Hook autoplay
commands/settingsmenu.py                                 # Autoplay settings
```

---

## ✅ Ready to Implement

All clarifications have been resolved. The implementation can begin with **Phase 1: Foundation**.

**Recommended Start**: `cache_manager.py` - this is the foundation that all other modules depend on.

---

*Document finalized: January 4, 2026*
*No further clarifications needed.*
