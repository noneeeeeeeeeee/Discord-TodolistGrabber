# Core Idea

Quality over speed. Users have to wait for a certain amount of time for cold start. While the bot initializes

> "Apple Music's algorithm analyzes a vast array of data points, from individual listening habits to broader trends in music consumption. This includes the genres a user prefers, the artists they listen to most, how often they play certain songs, and even the time of day they're most active."

---

## Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| BootstrapManager | ✅ Complete | Scenarios A/B/C with Last.fm integration, first-run protection |
| EnrichmentWorker | ✅ Complete | 4-stage pipeline with priority queue, 8s timer with reset |
| LastFMClient | ✅ Complete | Top tracks, similar, tag-based discovery |
| DeezerClient | ✅ Complete | Metadata fetch with BPM, gain, genres |
| Recommender P1 Wait | ✅ Complete | Waits for analysis_verified before scoring |
| Cache Sharding | ✅ Complete | enrichment_v3_*.json files, 500 entries each |
| BufferManager | ✅ Complete | Apple Music-style 5-track buffer with nuke on user add |
| TrackResolver | ✅ Complete | 3-tier waterfall search (Direct → Grounded → Last.fm) |
| MappingEntry.collaborators | ✅ Complete | Collaboration graph for artist expansion |

### Recent Fixes (December 2025)

1. **Scenario A 5→200 Fix**: First run now queues all 200 tracks (was limited to 5)
2. **First Run Protection**: `_first_run_triggered` flag prevents duplicate Scenario A runs
3. **Gemini 8s Timer**: Timer resets on new track, flushes at 50 or timeout, retries failed
4. **Scenario C Dedup**: Max 3 per artist, fill remaining with Scenario B tracks
5. **Buffer Nuke**: Clears buffer when user adds track to queue
6. **Collaborators Field**: Added to MappingEntry for recommendation graph expansion
7. **Gemini queue_enrichment()**: Added public method returning future (batch queuing without blocking)
8. **Enriched Count Methods**: `get_enriched_count()` and `get_gemini_enriched_count()` in CacheManager
9. **Daydream 200 Enriched Loop**: Now loops until 200 ENRICHED (analysis_verified=True), not just queued
10. **30-min Intervals After Bootstrap**: Uses fast intervals during bootstrap, 30-min after 200 enriched
11. **Deezer Gain Field**: Added `deezer_gain` to EnrichmentEntry for loudness data from Deezer API
12. **Deezer BPM/Gain Fix**: Now stores actual value (0 = unknown, None = not fetched)
13. **Pipeline Order Fix**: Changed from Deezer→Audio→Gemini to Deezer→Gemini→Audio
14. **Unified Pipeline**: BootstrapManager now uses EnrichmentWorker (was bypassing it)
15. **Cache Entry Creation**: Deezer stage creates initial cache entries for Gemini to update
16. **Gemini Batch Await**: Tasks now wait for Gemini batch flush before continuing to audio analysis
17. **Stale Queue Cleanup**: Queue older than 1 hour is cleared on startup to prevent duplicate skips
18. **Bootstrap Tracking Fix**: Only successfully queued tracks are marked as bootstrapped
19. **Deezer Waterfall Search**: 3-tier search (exact → fuzzy → artist top tracks) for better matching
20. **DeezerClient.search_artist()**: New method for artist search
21. **DeezerClient.get_artist_top_tracks()**: New method to fetch artist's top tracks

---

## Machine Learning Enrichment (4-Factor System)

The Enrichment includes 4 different factors from Librosa, EfficientAT, Deezer, and GeminiAPI. All are crucial and have NO fallbacks.

### 1. Librosa (Audio Structure Analysis)

- **BPM/Tempo**: Beats per minute for energy matching
- **Key/Scale**: Musical key detection for harmonic compatibility
- **Energy Curve**: How energy evolves through the song (intro → buildup → drop → outro)
- **Spectral Features**: Brightness, rolloff, centroid for tonal characteristics
- **Onset Strength**: Rhythmic intensity and groove patterns

### 2. EfficientAT (MobileNetV3 - Semantic Audio Tags)

- **Mood Tags**: Happy, sad, energetic, chill, aggressive, melancholic
- **Instrument Detection**: Guitar, synth, drums, piano, vocals, bass
- **Genre Classification**: Multi-label genre probabilities (pop: 0.8, electronic: 0.3)
- **Embedding Vector**: 512-dim vector for similarity calculations (cosine distance)

### 3. Deezer (Metadata & Audio)

- **Track Metadata**: Artist, album, release date, explicit flag, ISRC
- **Genre Tags**: Official Deezer genre classification
- **Preview URL**: 30-second HQ audio preview for analysis
- **Collaboration Artists**: Featured artists for graph expansion
- **Related Artists**: Deezer's built-in artist similarity

### 4. GeminiAPI (Cultural Context & Semantic Understanding)

- **Lyrical Themes**: Love, party, breakup, motivation, storytelling
- **Cultural Context**: Era, scene, movement (e.g., "90s grunge", "UK garage revival")
- **Mood Descriptors**: Natural language mood descriptions beyond simple tags
- **Artist Context**: Career phase, typical sound, notable collaborations
- **Use Cases**: "Good for workout", "late night vibes", "road trip energy"

### Enrichment Output (per track)

```json
{
  "track_id": "artist::title",
  "embedding": [0.12, -0.45, ...],  // 512-dim from EfficientAT
  "bpm": 128,
  "key": "C minor",
  "energy": 0.78,
  "valence": 0.45,
  "danceability": 0.82,
  "tags": ["electronic", "uplifting", "synth-heavy"],
  "mood": "euphoric but melancholic",
  "cultural_context": "2010s EDM festival anthem",
  "collaborators": ["Artist B", "Artist C"]
}
```

This carries out the process of prepping the music so it can be analyzed and recommended via the recommender. Metadata includes collaboration artists - if the user likes that artist, the recommender knows which collabs they've done to expand the pool

# Daydreamer (Background Research Crawler)

> Like a researcher finding topics - once it finds something interesting, it researches more until the paper is complete.

This daydreamer is needed as no music metadata is provided upon first bot start. Its focus is to expand the database and gather metadata per 50 tracks while keeping API limits low. It runs a job queue every 30min-1hr (configurable via .env, default 30 minutes).

## Daydreamer Behavior

**Genre-Weighted Exploration:**

- If telemetry shows users listen to pop more → prioritize pop exploration
- Dynamic genre balancing based on actual user preferences
- Tracks "heat" of each genre based on play counts

**Batch Processing:**

- Daydreams until reaching 50 tracks per batch
- If focused on pop → grab pop tracks depth-first until batch complete
- Next run: A) Continue exploring same vein, OR B) Choose another genre from list

## Cold Start Bootstrap (First Run)

Upon bot first boot:

1. Fetch pool of 50 popular tracks from Deezer charts
2. Ingest up to **500 tracks** on FIRST run
3. Autoplay is **DISABLED** until 500 tracks in cache
4. Total after first run: 550 tracks (500 bootstrap + 50 first batch)

**Skip Option for Owner:**

- Bot checks if user is owner running music command
- Prompt: "Process first run daydreamer in background?"
- If yes → queue songs in background, user can play immediately

## Cold Start Thresholds

| Cache Size | Autoplay State | Matching Mode                    |
| ---------- | -------------- | -------------------------------- |
| 0-100      | DISABLED       | Show "Building taste profile..." |
| 100-300    | LIMITED        | Conservative matching only       |
| 300-500    | BASIC          | Genre-based recommendations      |
| 500+       | FULL           | Embedding similarity unlocked    |

## New Releases Check

- Check every **1 month** for new releases
- If batch 21 runs and >1 month since last check → run new releases function
- If new releases fill 50 tracks → signal next batch to continue checking
- Otherwise, resume normal exploration

---

## Queue Backlog (5-Song Buffer)

> "Apple Music monitors how users interact: play-through rate, repeat listens, library adds, shares. The more positive interactions, the more Apple assumes the song is worth recommending."

Apple Music shows ~10 tracks in "Playing Next", but for Discord we target **5 songs** in the buffer queue.

### Buffer Behavior (Option A - Refillable Drink)

Like a waiter refilling your cup - when buffer drops below 5, start adding more:

| Buffer Size | Action                           |
| ----------- | -------------------------------- |
| 5 songs     | Full - no action needed          |
| 4 songs     | Start enriching 1 candidate      |
| 3 songs     | Enriching 2 candidates           |
| <3 songs    | Priority enrichment mode         |
| 0 songs     | User must wait (Quality > Speed) |

### Skip Behavior & Reversion

When user skips repeatedly:

1. **1-2 skips**: Normal - variance is expected
2. **3+ consecutive skips**: Pivot to different mood/energy, NOT artist ban
3. **Keep skipping**: Revert to "anchor artists" (safe zone)
4. **Even anchors skipped**: Try completely different mood family

> "If I keep skipping, it keeps trying then falls back to core artist selection."

### Queue Variance (Novelty Injection)

Buffer isn't all identical tracks - includes variance:

- **70%**: Familiar (artist played before OR embedding similarity >0.85)
- **30%**: Discovery (new artist with embedding similarity 0.6-0.8)
- **Never**: Similarity <0.5 (too jarring)

---

## Recommender & Feedback Loop

> "Apple Music reacts on how often you skip or listen repeatedly. Based on the feedback loop, the algorithm changes recommendations."

### User Behavior Signals (Weight)

| Signal                      | Weight | Meaning                                      |
| --------------------------- | ------ | -------------------------------------------- |
| **Play-through rate**       | HIGH   | Did they finish (>80%) or skip early (<15s)? |
| **Repeat listens**          | HIGH   | Coming back = strong positive signal         |
| **"More Like This"**        | +0.5   | Explicit positive feedback                   |
| **"Less Like This"**        | -0.5   | Track penalty, -0.2 to similar embeddings    |
| **Quick skip (<15s)**       | -0.3   | Negative signal for track                    |
| **2+ artist skips/session** | -0.1   | Light artist penalty (not ban)               |
| **Full listen + no skip**   | +0.2   | Passive positive signal                      |

### Skip Penalty Decay System

```
Track skip:     -0.3 affinity (decays over 7 days)
Artist skip x2: -0.1 affinity (per session, resets next session)
"Less Like This": -0.5 track, -0.2 similar embeddings
```

### Anchor Artist System

> "Artists that I hearted - it always recommends as the start for testing what mood you're into."

- Track artists with **>3 completed plays** in session history
- Use anchor artists as "mood probes" before exploring
- If user skips anchor artist: **strong signal to change mood entirely**
- Re-recommend anchor tracks as "safe zones" after exploration failures

### Session Momentum (Energy Flow)

Avoid jarring transitions:

- Track energy/BPM trend across last 5 songs
- Max allowed jump: **30 BPM** or **0.3 energy delta**
- Use "transition bridges" (medium-energy tracks) to shift genres
- Gradual drift: 5 BPM change per song is acceptable

---

## Waterfall Deezer Search System

> "Metadata is like the DNA of a song. It helps the algorithm match your music with listeners looking for something just like it."

The gemini_service.py needs cleanup. Changes to cache_manager and deezer_fetch required.

### Search Tier 1: Direct Search (Fastest)

Use Gemini-Flash to extract 3 search keywords from YouTube title:

**Example:**

```
YouTube: "Live To Live | Hazbin Hotel Season 2 | Prime Video"
Search:  "Live To Live Hazbin Hotel Season 2"
Result:  Usually 1st or 2nd Deezer result
```

**Confidence Check:**

- Title similarity score (Levenshtein distance)
- Artist name match if available
- Duration within ±10 seconds

### Search Tier 2: Grounded Search (Fallback)

If Tier 1 fails after 3 attempts:

- Use Gemini 2.5-Flash + Google Search grounding
- Provide last 3 failed queries for context
- Ask: "What keywords should find this song on Deezer?"

### Search Tier 3: Give Up (Last Resort)

If all fails → song likely isn't a song:

- YouTube podcast, interview, ambient soundscape
- **Disable autoplay for session**
- **Unlock VIP slot** for other users

> **NOTE:** Deezer has ~129 million songs. If we can't find it, the waterfall needs improvement - it's almost certainly there.

---

## Daydreamer Pool (Last.fm → Deezer Pipeline)

Last.fm to Deezer is easier - Last.fm is already formatted correctly and maps well to Deezer search.

### Pool Temperature System

| Temperature | Description               | Use Case         |
| ----------- | ------------------------- | ---------------- |
| **Hot**     | Currently trending/viral  | Quick engagement |
| **Warm**    | Related to user history   | Personalization  |
| **Cold**    | New exploration territory | Discovery        |

---

## Daydreamer Workflow (Detailed)

### Scenario A: Bot From Zero (Cold Start)

```
Track finishes playing
    ↓
Not enriched yet? → Fetch Deezer preview (or check mapping cache)
    ↓
Start P1 Enrichment → Meanwhile, Last.fm builds pool (cold/warm/hot)
    ↓
Pool filtered → Check if any tracks already enriched in cache
    ↓
Batch to Gemini (max 50 tracks)
    ↓
Deezer fetches previews
    ↓
3 Worker Queues start enriching
```

**Enrichment Workers:**

1. Worker 1: Librosa analysis (BPM, key, energy)
2. Worker 2: EfficientAT embeddings
3. Worker 3: Gemini cultural context

This takes time, but quality > speed. Gets faster as cache builds.

### Scenario B: Cache at 500+ Tracks

Pool system can now:

- Check existing embeddings for similarity
- Skip enrichment for known tracks
- Use vector search for candidates
- Faster recommendations, less API calls

---

## Contextual Awareness (NEW)

> "Apple Music considers the time of day you listen to music."

### Time-Based Bias

| Time     | Energy Bias  | Example            |
| -------- | ------------ | ------------------ |
| 6am-10am | +0.1 energy  | Morning motivation |
| 10am-2pm | Neutral      | Focus/work         |
| 2pm-6pm  | +0.05 energy | Afternoon boost    |
| 6pm-10pm | Neutral      | Evening variety    |
| 10pm-2am | -0.15 energy | Night chill        |
| 2am-6am  | -0.2 energy  | Late night ambient |

### Session Context

- **Long sessions** (>1hr): Allow more exploration
- **Short sessions** (<15min): Stick to safe picks
- **Skip velocity**: Rapid skips = user indecisive, slow recommendations down

---

## Similarity Thresholds (NEW)

| Cosine Similarity | Relationship        | Action                 |
| ----------------- | ------------------- | ---------------------- |
| >0.95             | Nearly identical    | Avoid (too repetitive) |
| 0.85-0.95         | Very similar        | Safe recommendation    |
| 0.70-0.85         | Similar mood/energy | Good discovery         |
| 0.50-0.70         | Moderate similarity | Risky but possible     |
| <0.50             | Different           | Never recommend        |

---

## Collaboration Graph (NEW)

```
User likes Artist A
    ↓
Artist A features Artist B on 2 tracks
    ↓
Artist B gets +0.2 affinity boost
    ↓
Tracks where A+B collaborate: +0.4 boost
    ↓
Build "collaboration clusters" for genre bridging
```

This enables discovering new artists through trusted connections.

---

## V3 Modular Architecture (Factory Worker Pattern)

The V3 system is divided into **three independent modules** that operate as a factory pipeline:

### Module 1: BootstrapManager (Daydreamer)

**File:** `bootstrap_manager.py`
**Status:** ✅ Implemented

**Responsibility:** Only manages track discovery and queue filling. Does NOT perform enrichment.

**Key Features:**

- Uses `DaydreamScenario` enum for state management
- Integrates `LastFMClient` for chart/similar/tag APIs
- Persists state between restarts
- Pauses when user sessions are active (P1/P2 have priority)

**Scenarios:**

#### Scenario A: First Run (Empty Cache)

```text
Cache empty → Fetch 200 tracks from Last.fm chart.getTopTracks
    ↓
Verify each via Deezer → Get preview URL + metadata
    ↓
Add to enrichment queue with P3 (DAYDREAM) priority
    ↓
Done. Wait for next batch cycle.
```

#### Scenario B: Daydreaming (Cache > 200 tracks)

```text
Cache has data → Get seed tracks from recent sessions
    ↓
Use Last.fm track.getSimilar + tag.getTopTracks
    ↓
Verify via Deezer → Get preview URLs
    ↓
Add to enrichment queue (P3 priority)
```

#### Scenario C: New Releases Check (Monthly)

```text
>30 days since last check → Fetch from Deezer editorial/releases
    ↓
Filter tracks with preview URLs
    ↓
Add to enrichment queue (P3 priority)
    ↓
Continue if batch full, else resume Scenario B
```

**Queue Limit:** Max 200 tracks in processing queue at any time.

---

### Module 2: EnrichmentWorker (Factory Pipeline)

**File:** `enrichment_worker.py`
**Status:** ✅ Implemented

**Responsibility:** Process tracks from queue through the 4-stage enrichment pipeline. Runs independently with priority support.

**Priority System:**

| Priority | Value | Use Case | Behavior |
|----------|-------|----------|----------|
| USER | 1 | Track user is playing | Immediate, blocks until complete |
| BUFFER | 2 | Buffer refill | High priority, 30s wait max |
| DAYDREAM | 3 | Background crawling | Lowest, pauses for P1/P2 |

**Pipeline Stages:**

```text
┌─────────────────────────────────────────────────────────────┐
│                    ENRICHMENT PIPELINE                       │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  STAGE 1: Deezer Resolution + Metadata                      │
│  ┌────────────────────────────────────────────────────┐     │
│  │ search_track(artist, title) → DeezerTrack          │     │
│  │ get_track_details(id) → Full metadata:             │     │
│  │   - preview_url (30s HQ audio)                     │     │
│  │   - bpm, gain (dB), explicit, isrc, genres         │     │
│  │ → Create MappingEntry + EnrichmentEntry            │     │
│  └────────────────────────────────────────────────────┘     │
│                           ↓                                  │
│  STAGE 2: Audio Analysis (Librosa + EfficientAT)            │
│  ┌────────────────────────────────────────────────────┐     │
│  │ Download preview → queue_analysis()                 │     │
│  │ → 5D simple_vibe: [energy, valence, danceability,   │     │
│  │    acousticness, brightness]                        │     │
│  │ → 4D flow: [loudness, tempo, key, mode]             │     │
│  │ → 512D-2048D EfficientAT embedding                  │     │
│  │ → Store: analysis_verified = True                   │     │
│  └────────────────────────────────────────────────────┘     │
│                                                              │
│  PARALLEL: Gemini Cultural Enrichment (Batched)             │
│  ┌────────────────────────────────────────────────────┐     │
│  │ Batch queue (artist + title only)                   │     │
│  │ → Wait for 50 tracks OR 8s timeout                  │     │
│  │ → Single API call → tags, mood, activity, daypart   │     │
│  └────────────────────────────────────────────────────┘     │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

**Key Methods:**

```python
# Add single track (used by Recommender for P1)
await worker.add_task(artist, title, Priority.USER, source="user")

# Batch add (used by BootstrapManager for P3)
await worker.add_batch(tracks, Priority.DAYDREAM, source="lastfm")

# Wait for P1 enrichment (blocks until complete)
success = await worker.enrich_for_user(artist, title, timeout=30.0)

# Wait for buffer batch (partial completion ok)
count = await worker.enrich_batch_for_buffer(tracks, max_wait=30.0)
```

**Duplicate Filtering:** Before any track enters the pipeline:

1. Check if track_key exists in processing queue (skip if present)
2. Check if track_key exists in enrichment cache with `analysis_verified=True`

---

### Module 3: Recommender (Wait for Enrichment)

**Files:** `contextual_recommender.py`, `__init__.py` (`_prepare_candidates`)
**Status:** ✅ Implemented

**Responsibility:** Wait for candidates to be enriched, then calculate recommendations.

**Key Behavior: P1 Wait for Enrichment**

Per design: *"If not enriched, process it as prio 1. User waits."*

```python
# In _prepare_candidates():
# After fetching candidates, check analysis_verified
candidates_needing_analysis = [
    (artist, title) for prepared_entry in prepared
    if not enrichment.get("analysis_verified")
]

# Request P1/P2 enrichment and wait
if candidates_needing_analysis:
    enriched_count = await worker.enrich_batch_for_buffer(
        candidates_needing_analysis,
        max_wait=30.0,  # User waits up to 30s
    )
    # Re-fetch enrichment data for newly enriched candidates
```

**Recommendation Flow:**

```text
User requests autoplay
    ↓
Build candidate pool from cache + Last.fm similar artists
    ↓
Check: Do candidates have analysis_verified = True?
    ↓
NO → Request BUFFER priority enrichment (P2)
    → Wait up to 30s for audio analysis
    → Re-fetch embeddings/flow vector
    ↓
YES → Skip to scoring
    ↓
Calculate similarity scores using:
    - computed_embedding (512D-2048D cosine similarity)
    - computed_simple_vibe (5D Euclidean distance)
    - Flow vector (BPM, key compatibility)
    - Cultural tags overlap
    ↓
Apply user preferences (feedback history, skip penalties)
    ↓
Return top candidates for 5-song buffer
```

**Cold Start Thresholds (Progressive Logic):**

| Cache Size | Mode | Behavior |
|------------|------|----------|
| 0-100 | DISABLED | Show "Building taste profile..." |
| 100-300 | LIMITED | Conservative matching only |
| 300-500 | BASIC | Genre-based recommendations |
| 500+ | FULL | Embedding similarity unlocked |

---

### Module Interaction Diagram

```
┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  TRACK FETCHER   │     │ ENRICHMENT       │     │   RECOMMENDER    │
│  (Daydreamer)    │     │ WORKER           │     │                  │
├──────────────────┤     ├──────────────────┤     ├──────────────────┤
│                  │     │                  │     │                  │
│ Last.fm/Deezer   │────▶│ Processing Queue │     │ User Request     │
│ Charts/Similar   │     │ (max 200 tracks) │     │     ↓            │
│                  │     │      ↓           │     │ Build Pool       │
│ Add tracks if:   │     │ Deezer  → Preview │     │     ↓            │
│ - queue < 200    │     │      ↓  & Metadata│     │ Filter by        │
│ - not duplicate  │     │ Librosa+AT       │     │ analysis_verified│
│                  │     │      ↓           │     │     ↓            │
│ Scenarios:       │     │ Cache Entry      │────▶│ Score + Rank     │
│ A: First run     │     │ verified=True    │     │     ↓            │
│ B: Daydream      │     │                  │     │ 5-Song Buffer    │
│ C: New releases  │     │ [Parallel]       │     │                  │
│                  │     │ Gemini Batch     │     │                  │
│                  │     │ (50 max, 8s wait)│     │                  │
└──────────────────┘     └──────────────────┘     └──────────────────┘
```

---

### Configuration Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DAYDREAM_INTERVAL` | 1800 (30min) | Seconds between daydream cycles |
| `DAYDREAM_BATCH_SIZE` | 50 | Tracks per daydream cycle |
| `PROCESSING_QUEUE_MAX` | 200 | Max tracks in processing queue |
| `FIRST_RUN_FETCH_COUNT` | 200 | Tracks to fetch on first run |
| `NEW_RELEASE_CHECK_DAYS` | 30 | Days between new release checks |
| `GEMINI_BATCH_SIZE` | 50 | Max tracks per Gemini API call |
| `GEMINI_BATCH_TIMEOUT` | 8.0 | Seconds to wait before flushing batch |
| `BUFFER_TARGET_SIZE` | 5 | Target songs in autoplay buffer |
| `ENRICHMENT_SHARD_MAX_ENTRIES` | 500 | Max entries per enrichment shard |

---

### Cache Sharding (Enrichment)

**File:** `cache_manager.py`
**Status:** ✅ Implemented

Enrichment cache is split into multiple shard files to prevent single large file issues:

```text
cache/music/
├── enrichment_v3_0.json    (≤500 entries)
├── enrichment_v3_1.json    (≤500 entries)
├── enrichment_v3_2.json    (≤500 entries)
└── ...
```

**Key Features:**

- **Automatic Migration**: Legacy `enrichment_v2.json` auto-migrates to shards on first load
- **Fast Lookup**: Key-to-shard mapping (`_enrichment_key_to_shard`) for O(1) lookups
- **Incremental Saves**: Only the modified shard is saved, not all shards
- **Backward Compatible**: `_enrichment_cache` property still works for legacy code

**Shard Assignment Logic:**

```python
# When adding a new entry:
for shard_id, shard_data in shards.items():
    if len(shard_data) < 500:  # SHARD_MAX_ENTRIES
        shard_data[key] = entry
        return shard_id

# All shards full → create new shard
new_shard_id = next_shard_id
shards[new_shard_id] = {key: entry}
```

**Cache Stats Output:**

```json
{
  "enrichment": 1523,
  "enrichment_shards": 4,
  "enrichment_shard_details": {
    "shard_0": {"entries": 500, "size_kb": 412.5},
    "shard_1": {"entries": 500, "size_kb": 398.2},
    "shard_2": {"entries": 500, "size_kb": 405.1},
    "shard_3": {"entries": 23, "size_kb": 18.9}
  }
}
```

---

### User Request Priority Flow

```
User plays song → Autoplay triggered
    ↓
Check: Is current track enriched?
    ↓
NO → P1 Enrichment (user waits)
    → Deezer lookup
    → Audio analysis
    → Gemini cultural (added to batch)
    → analysis_verified = True
    ↓
YES → Skip to recommendation
    ↓
Build candidate pool
    ↓
Check: Are all candidates enriched?
    ↓
Some missing → Wait for P1 enrichment of candidates
    → Enrichment Worker processes with high priority
    ↓
All ready → Calculate scores → Fill 5-song buffer
    ↓
Buffer fills → User continues listening
    → New songs added as buffer depletes
    → As cache grows, wait times decrease
```

**Key Insight:** Early sessions require patience (cold start). As cache builds:

- More tracks pre-enriched by Daydreamer
- Faster recommendations
- Eventually near-instant autoplay

use <https://r.jina.ai/{URL}> if the url cannot be accessed

---

## Recent Fixes (Session: Dec 3, 2025)

### Fix 22: Batch Deezer Resolution with Gemini Fallback

**File:** `track_resolver.py`
**Status:** ✅ Implemented

Added `resolve_batch_to_deezer()` method for batch resolution of Last.fm tracks to Deezer:

```python
# Phase 1: Waterfall search (3 strategies per track)
# - Exact search: "{artist} {title}"
# - Fuzzy search: title only, filter by artist similarity  
# - Artist top tracks: search artist, get top 50, match title

# Phase 2: Gemini batch fallback (max 50 tracks)
# - Sends failed tracks to Gemini for refined search queries
# - Each track gets 3 alternative queries
# - Retries on Deezer with Gemini suggestions
```

**New Methods:**

- `resolve_batch_to_deezer()`: Main batch resolver entry point
- `_waterfall_deezer_search()`: 3-tier search strategy
- `_find_best_deezer_match()`: Jaccard similarity matching
- `_gemini_batch_resolve()`: Gemini batch query generation
- `resolve_youtube_to_deezer_simple()`: Simplified YouTube→Deezer (like !p)

### Fix 23: Bootstrap Manager Phase 2 Integration

**File:** `bootstrap_manager.py`
**Status:** ✅ Implemented

Both `_scenario_first_run()` and `_scenario_daydreaming()` now use 2-phase resolution:

```python
# Scenario A: First Run (200 tracks from Last.fm)
Phase 1: Direct Deezer search for each track
Phase 2: Batch resolver for failed tracks (with Gemini)

# Scenario B: Daydreaming (exploration)
Phase 1: Similar tracks + tag discovery → Deezer search
Phase 2: Batch resolver for failed tracks (with Gemini)
```

**Result:** More tracks successfully mapped (fewer skips due to failed Deezer search)
