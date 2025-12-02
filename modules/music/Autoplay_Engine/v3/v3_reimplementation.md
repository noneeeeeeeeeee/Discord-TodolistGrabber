# Core Idea

Quality over speed. Users have to wait for a certain amount of time for cold start. While the bot initializes

> "Apple Music's algorithm analyzes a vast array of data points, from individual listening habits to broader trends in music consumption. This includes the genres a user prefers, the artists they listen to most, how often they play certain songs, and even the time of day they're most active."

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
