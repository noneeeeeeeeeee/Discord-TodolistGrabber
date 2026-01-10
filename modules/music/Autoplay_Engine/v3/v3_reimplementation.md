
## Core Philosophy

**Quality over speed.** The engine waits for sufficient data before making recommendations during cold start while showing real-time progress updates.

### Design Principles

1. **Modular Independence**: Each module operates independently with well-defined interfaces
2. **Graceful Degradation**: Gemini is optional - engine works with Deezer/Last.fm alone
3. **Async-First**: All heavy operations are non-blocking
4. **Session Isolation**: Each guild session is independent (no cross-session data leakage)
5. **Cache Efficiency**: Sharded JSON files with version tracking for schema migrations
6. **V1 Removed**: V3 is the only engine - no backwards compatibility with V1

---

## Configuration

### Environment Variables

```env
# Required
LASTFM_API_KEY=your_key              # Last.fm API key (required for recommendations)

# Optional  
GEMINI_API_KEYS=["key1","key2"]      # Gemini API keys as JSON array (optional, enhances analysis)
AUTOPLAY_V3_VERBOSITY=1              # 0=errors, 1=overview, 2=detailed, 3=debug
MAX_CONCURRENT_SESSIONS=10           # Maximum autoplay sessions (default: 10)
```

### Verbosity Levels

| Level | Description | Use Case |
|-------|-------------|----------|
| 0 | Errors only | Production (minimal logs) |
| 1 | Overview (default) | Normal operation (Debugging) |
| 2 | Detailed | Debugging issues (Debugging) |

### Module Versioning

```python
ENGINE_VERSION = "3.0.0"   # Engine version
MAPPING_VERSION = 1           # Song mapping format version
METADATA_VERSION = 1          # Song metadata format version
SESSION_VERSION = 1           # Session state format version
```

---

## Architecture Overview

```
                    ┌─────────────────────────────────────────────┐
                    │              LastFMAutoplayV3               │
                    │         (V1-Compatible Interface)           │
                    └─────────────────┬───────────────────────────┘
                                      │
                    ┌─────────────────▼───────────────────────────┐
                    │                V3Engine                     │
                    │         (Orchestration Layer)               │
                    └─────────────────┬───────────────────────────┘
                                      │
    ┌────────────┬────────────┬───────┴───────┬────────────┬────────────┐
    ▼            ▼            ▼               ▼            ▼            ▼
┌────────┐ ┌──────────┐ ┌───────────┐ ┌────────────┐ ┌─────────┐ ┌────────────┐
│Session │ │  Buffer  │ │Recommender│ │   Song     │ │ Cache   │ │ Daydreamer │
│Manager │ │  Manager │ │           │ │  Analyzer  │ │ Manager │ │            │
└────────┘ └──────────┘ └───────────┘ └────────────┘ └─────────┘ └────────────┘
     │           │            │              │            │            │
     └───────────┴────────────┴──────────────┴────────────┴────────────┘
                                      │
                              ┌───────▼───────┐
                              │   Event Bus   │
                              │  (Pub/Sub)    │
                              └───────────────┘
```

---

## Startup Sequence

1. `music_player.py` creates `LastFMAutoplayV3` instance
2. `is_available()` checks for `LASTFM_API_KEY` (synchronous - returns immediately)
3. `start()` is called by music_player, triggering background initialization:
   - Configure logging via `configure_v3_logging()`
   - Initialize all V3 components asynchronously
   - Start Daydreamer background exploration
4. First recommendation request completes any remaining initialization

---

## Core Modules

### Session Manager (`session_manager.py`)

Manages active autoplay sessions with configurable capacity via `MAX_CONCURRENT_SESSIONS` env var.

**Session States:**

| State | Song Count | Behavior |
|-------|------------|----------|
| COLD | 0-10 | Building context, cautious recommendations |
| WARM | 10-25 | Stable preferences established |
| HOT | 25-50 | Confident diversification |
| EXTENDED | 50+ | Long session, balanced exploration |

**Key Feature:**

The session state is not dependent on song count and is dynamic. It will adjust the state of the pool currently based on user behaviour

---

### Buffer Manager (`buffer_manager.py`)

Maintains a 5-song look-ahead buffer (Apple Music style).

**Buffer Slot Types:**

- **SAFE**: Songs matching established preferences
- **EXPLORATORY**: Songs testing new genres/artists

The buffer slot types is dynamic and slot distribution phase will change depending on user behaviour. It might go more safe or more confident depending on what it knows about the user(s) in the current session

**Slot Distribution by Phase:**

| Phase | Songs | SAFE | EXPLORATORY |
|-------|-------|------|-------------|
| Early | 0-5 | 4 | 1 (80% safe) |
| Establishing | 5-15 | 3 | 2 (60% safe) |
| Confident | 15+ | 2 | 3 (40% safe) |

Buffer clears when user queues a song after autoplay, forcing context re-analysis.

---

### Song Analyzer (`song_analyzer.py`)

Three-layer audio analysis using parallel worker pools.

**Analysis Layers:**

1. **Physics Layer (Librosa)** - Always available

   ```python
   PhysicsLayer:
     bpm: float
     key: str  
     mode: str  # "major" or "minor"
     loudness_db: float
     energy: float
     spectral_centroid: float
     spectral_rolloff: float
     mfcc_coefficients: List[float]  # 13 coefficients
     zero_crossing_rate: float
   ```

2. **Semantics Layer (EfficientAT)** - Requires model download
   - Neural audio embeddings
   - Instrument tags with probabilities
   - Quality score

3. **Librarian Layer (Gemini)**
   - Canonical title/artist
   - Release era, cultural vibe, micro-genre
   - Explicit content flag

**Priority Levels:**

| Priority | Use Case |
|----------|----------|
| 1 (IMMEDIATE) | Active session needs |
| 2 (BUFFER) | Buffer filling |
| 3 (DAYDREAMER) | Background exploration |

**Worker Pool Architecture:**

- ThreadPoolExecutor with configurable workers (default: 3)
- Bulk Gemini processing with batching (50 songs/batch, 5s timeout)

As to save on gemini requests. The gemini processing is processed in parralel with the MachineLearning models. This is to ensure: Stability and Performance. Where it will finish faster than the machine learning models and not have to wait for the batch to fill up.

---

### Daydreamer (`daydreamer.py`)

Background exploration when no active sessions.

**Exploration Strategies:**

1. Genre Balancing - Fill underrepresented genres
2. Chart Exploration - Discover popular tracks from Deezer charts
3. Artist Expansion - Find similar artists via Last.fm
4. Random Discovery - Serendipitous exploration

**Rate Limiting:**

| Cache Size | Exploration Rate |
|------------|------------------|
| < 5000 songs | 100 songs / 30 min |
| ≥ 5000 songs | 50 songs / hour |

**Invocation:** Started automatically via `LastFMAutoplayV3.start()` when music_player initializes.
This depends on either if the last batch has ran within the allocated time yet. If a batch has ran in the past x minutes/hours it will check and not run it upon bot crash/restart.

---

### Cache Manager (`cache_manager.py`)

Sharded JSON storage with version tracking.

**Cache Location:** `./cache/Autoplay/v3/`

**Subdirectories:**

- `mappings/` - Song ID mappings (Deezer ↔ YouTube ↔ Last.fm)
- `metadata/` - Analyzed song metadata
- `sessions/` - Session state persistence
- `daydreamer/` - Exploration state

**Sharding:** Files shard at 5000 entries using suffix pattern: a, b, ..., z, aa, ab, ...

---

### Mappings Manager (`mappings.py`)

Resolves songs across platforms (Deezer ↔ YouTube ↔ Last.fm).

**Resolution Chain:**

1. Check cache for existing mapping
2. Search Deezer API
3. Parse YouTube title via Gemini (if available)
4. Fallback: Google grounding search

**Key Insight:** Deezer is the source of truth for audio previews (30s) needed for analysis.

**Extra Information**:
The Youtube > Deezer & Last.fm mapping will use gemini to parse the youtube title and channel with gemini-2.5-flash-lite. In order to ensure accuracy. I've tried with various heuristics method to no Avail. This is the most accurate way to map if you only know the youtube title.

Use case:

1. When the user plays the song and the recommender needs to map it. << The only source is via youtube that the user can play.

---

### Gemini Manager (`gemini_manager.py`)

**Features:**

- Multi-key rotation (JSON array: `["key1", "key2"]`)
- Rate limit handling with backoff
- Batch processing to reduce API calls
- Model selection (flash-lite for parsing, flash for analysis)

**Initialization:** Resilient - logs warning if unavailable but doesn't block engine startup.

---

## Skip Detection & Telemetry

Skip detection uses progress ratio:

```python
was_skipped = progress_ratio < 0.9  # < 90% = skip

# Button feedback overrides:
if feedback_type == "less_like_this":
    was_skipped = True   # Treat as skip
elif feedback_type == "more_like_this":
    was_skipped = False  # Treat as full listen
```

The skip deetection can also use a more feedback based skip
where if the song reaches

1. 20% it will penelize more
2. if its at 50% it wil penelize less
3. And so on

---

## The "Restaurant" Model

How modules work together:

| Role | Module | Responsibility |
|------|--------|----------------|
| Head Chef | Recommender | Final song selection |
| Waiter | Context Tracker | Customer feedback |
| Sous Chef 1 | Buffer Manager | Prep work (lookahead) |
| Sous Chef 2 | Novelty Controller | Creative direction |
| Ingredient Specialist | Vector Search | Audio similarity |
| Customer Expert | Collaborative Recommender | Behavior patterns |
| Background Staff | Daydreamer | Stocking ingredients |

---

## Context Analyzer Logic

**Vibe Momentum:**

- Decaying weights: Most recent song = 50% of "vibe", previous 4 = remaining 50%
- Allows faster pivoting when group taste shifts

**Skip Pattern Analysis:**

| Skips | Interpretation |
|-------|----------------|
| 1 | Not feeling it right now |
| 2 | Not this artist/mood |
| 3+ | Don't like this genre |

**Artist Cooldown:** Soft-ban to prevent same artist playing too frequently.
**The skip Interperation**:
The skip interpertation needs work. For now it is fine as-is
---

## API Dependencies

| API | Purpose | Required |
|-----|---------|----------|
| Deezer | Song search, 30s previews, metadata | ✅ Yes |
| Last.fm | Similar tracks, artist info | ✅ Yes |
| Gemini | Parsing, cultural analysis |  ✅ Yes |
| YouTube | Playback URLs | Via Lavalink |

---

## First-Start Dependencies

On first run, the bot downloads required dependencies:

1. **FFmpeg** - Audio processing (auto-downloaded)
2. **EfficientAT Model** - Neural embeddings (~100MB)
   - URL: `https://github.com/fschmid56/EfficientAT/releases/download/v0.0.1/mn10_as_mAP_471.pt`

Progress bar shown during download.

---

## Limitations

1. **Session Isolation**: Each session starts fresh (different users in guild)
2. **Preview Dependency**: Analysis requires Deezer 30s preview
3. **YouTube Mapping**: Complex parsing required for user-queued tracks
4. **No V1 Fallback**: V1 has been removed - V3 only

---

## Optimizations Implemented

### Dynamic Look-Ahead Buffering

- Analyzes next 5-10 candidates while current song plays
- Emergency refill when buffer < 3 songs

### Cross-Session Cache

- Acoustic analysis (BPM, vectors) retained across sessions
- Only behavioral context resets per session
- Drastically reduces cold start for popular songs

### Seed-Centric Prioritization

- Prioritize candidates closest to current queue in vector space
- Analyzing 100 fitting songs > 1000 random songs

### Failure Fallbacks

- Skip failed Deezer previews immediately (don't retry)
- Keep progress moving for user

---

## URLs & References

- EfficientAT Model: `https://github.com/fschmid56/EfficientAT/releases/download/v0.0.1/mn10_as_mAP_471.pt`
- Librosa: `https://librosa.org/doc/latest/install.html`
- Gemini API: `https://ai.google.dev/gemini-api/docs`
- Last.fm API: `https://www.last.fm/api`
- Deezer API: `https://developers.deezer.com/api`
- Jina Reader (for docs): `https://r.jina.ai/{url}`
