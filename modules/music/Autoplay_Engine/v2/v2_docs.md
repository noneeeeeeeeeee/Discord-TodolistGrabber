# Autoplay Engine V2 DocumentationThe plan.md is missing some still from issues #1-#3

## Phase SummaryOK here are the issues discussed and resolve: Make it into a plan.md if its too long split it

| Phase | Status | Notes |Excellent — now we’re entering the **core intelligence layer** of your system: the **Recommendation Algorithm / Novelty Engine** (Issue #3).

| ----- | ------ | ----- |

| Foundations | ✅ Complete | Gemini/Last.fm prerequisites audited and documented. |You’ve already got the scaffolding for a solid hybrid recommender (Last.fm + heuristics + Gemini metadata + feedback multipliers). What’s missing is **balance and stability** — right now it occasionally nails it but other times jumps genres or repeats wrong clusters because the novelty and diversity mechanisms aren’t tuned like Spotify/Apple/YouTube’s.

| Phase 1 · Gemini Service | ✅ Complete | Managed Gemini client with batching, quota tracking, and cache hydration in place. |

| Phase 2 · Collaborative Telemetry | ✅ Complete | Telemetry capture, collaborative snapshot loader, and recommender blend rewired. |Let’s build this step by step and model it after how those real systems actually behave.

| Phase 3 · Pomice Integration | ⚠️ Partial | Resolver + cache implemented, smoke-test harness still pending. |

| Phase 4 · Mood & Arc Enhancements | ⚠️ Partial | Mood vectors plumbed through scoring; architecture docs still need refresh. |---

| Phase 5 · QA & Observability | ⚠️ Partial | Runtime stats/logging landed; runbook yet to be authored. |

| Phase 6 · Follow-ups | ⚠️ In Progress | Offline CF stub shipped; unit tests & dataclass refactor outstanding. |## 🎯 **ISSUE #3 – Recommendation / Novelty Algorithm**

See `v2_plan.md` for a checkbox-level task breakdown that mirrors this table.### 🧩 **Goal**

---Create a _dynamic, context-aware_ recommendation loop that:

## Implemented Subsystems1. Feels consistent (“the same vibe”) yet non-repetitive.

2. Adapts smoothly to feedback.

### Gemini Service — `modules/music/Autoplay_Engine/v2/gemini_service.py`3. Introduces discovery and rediscovery in a natural arc.

- Key rotation with cooldown windows; supports multiple API keys via `GeminiApiKeys`.4. Uses context (recent sequence, genre clusters, time, feedback bias) to steer selection.

- Persistent quota ledger stored at `cache/music/gemini_usage.json` with automatic daily rollover.

- Batched enrichment queue (`ENRICHMENT_BATCH_MAX` configurable) to minimise request volume.---

- Shared helpers for metadata parsing (`parse_track_metadata`), enrichment, and mood vector classification.

- Structured logging around quota exhaustion, key failures, and batch outcomes.## 🧠 **What Apple Music / Spotify / YouTube actually do**

### Cache Manager — `modules/music/Autoplay_Engine/v2/cache_manager.py`| Service | Core Model | Behavior |

- Typed dataclasses for mappings, enrichment payloads, Gemini parse results, mood vectors, and collaborative snapshots.| ----------------- | ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |

- TTL enforcement (mapping 360d, enrichment 180d, parsing 120d, mood vectors 180d).| **Spotify** | Hybrid: collaborative embeddings + content features | Keeps 70 % songs within current cluster, 20 % similar cluster, 10 % novelty. Novelty is distance-bounded and timed to user engagement. |

- Persists collaborative embeddings to `cache/music/collaborative_embeddings.json`.| **Apple Music** | Cluster + genre graph with editorial bias | Strong “mood consistency” weighting — prefers timbral/genre cohesion over raw similarity. Novelty introduced after 3–5 songs. |

- Exposes `get_cache_stats()` consumed by the runtime stats surface.| **YouTube Music** | User-action loop (likes, skips, completion) + content graph | Uses skip-rate prediction → penalizes songs likely to be skipped. Novelty spikes when skip-rate drops (user is satisfied). |

### Feedback & Telemetry — `modules/music/Autoplay_Engine/v2/feedback_manager.py`---

- Hashes guild/user ids with per-install salt, retains rolling in-memory buffers, and streams JSONL events to `cache/music/telemetry/events.jsonl`.

- Supports guild/user opt-out flags and event coercion for unknown types.## ⚙️ **Root Cause in Your Current System**

- Async-safe writer with dedicated I/O lock for multi-task usage.

1. **Anti-repetition safeguard** sometimes over-filters → removes correct candidates.

### Collaborative Matrix — `modules/music/Autoplay_Engine/v2/collaborative_matrix.py`2. **Novelty multiplier** too random — doesn’t respect cluster distance or recency.

- Loads collaborative embeddings from cache or disk, normalises vectors, and provides cosine similarity helpers.3. **Feedback multipliers** not yet tightly coupled with novelty sampling.

- Metadata snapshot summarises vector counts and train timestamp for observability.4. **No temporal rhythm** — the system doesn’t “plan ahead” a 5-song arc (discovery pacing).

### Contextual Recommender — `modules/music/Autoplay_Engine/v2/contextual_recommender.py`---

- Blends content, session, novelty, mood alignment, and collaborative similarity (default weight 0.3).

- Guild-scoped toggles for enabling/disabling collaborative blend and overriding weights.## 🧱 **Proposed Architecture – “Contextual Arc Recommender”**

- Returns per-track breakdowns for logging/debug surfaces.

The algorithm maintains three conceptual layers:

### Track Resolver — `modules/music/Autoplay_Engine/v2/track_resolver.py`

- Attempts to rebuild Pomice `Track` objects from cached mappings, falling back to live search.```

- Persists successful resolutions with channel metadata and verified flags to accelerate future lookups.RecentContext → Cluster Engine → Candidate Mixer

- Emits structured debug logs for cache hits, misses, and search outcomes.```

### Autoplay Orchestrator — `modules/music/Autoplay_Engine/v2/__init__.py`Each layer refines and balances the next.

- `LastFMAutoplayV2` mirrors the v1 interface so `MusicPlayer` can toggle engines without extra glue.

- Fetches seed metadata via Gemini, gathers Last.fm similar tracks, enriches candidates, and ranks them with `ContextualRecommender`.### 🌈 Phase 4 Mood Vector Pipeline

- Uses `TrackResolver` to return ready-to-queue Pomice tracks; records feedback events through `FeedbackManager`.

- Maintains per-guild history to penalise recent reruns and exposes `clear_history()`.- `AutoplayEngineV2.get_mood_vector` now calls `GeminiService.classify_mood_vector` and persists the response as a `MoodVectorEntry` in `CacheManager` (energy, valence, tempo, confidence, label).

- `enrich_track` automatically requests a mood vector after tags are fetched so the enrichment payload returns a cached `mood_vector` alongside tags/mood/energy.

### Offline Tooling — `scripts/cf_train.py`- `ContextualRecommender.score_candidates` accepts a `session_mood_vector` and candidate mood vectors to compute:

- Lightweight collaborative embedding stub that aggregates telemetry play/skip counts and emits a snapshot compatible with `CollaborativeMatrix` hydration. - `mood_alignment` (1 − normalized distance) blended into the base score at 0.15 weight.

- Safe to run on fresh installs (tolerates missing telemetry files) and writes to `modules/music/Autoplay_Engine/v2/cache/collaborative_embeddings.json` by default. - A novelty term that mixes raw novelty with mood distance (60/40) to pace exploration.

  - Optional `target_mood` bonuses for planned arcs.

---- Breakdown data now surfaces `mood_alignment` and `mood_distance` so arc planning logs can explain mood-driven choices.

## Configuration & Runtime Behaviour---

- Global toggle lives in `modules/music/Autoplay_Engine/config.py` (`AUTOPLAY_VERSION`). Default is `"v2"`.### 1️⃣ **RecentContext Tracker**

- When V2 initialisation fails (missing Gemini key, import error, etc.) the config now logs and falls back to the legacy engine so autoplay stays operational.

- `supports_feedback_buttons()` only returns `True` when V2 is both selected and available; the UI hides "More/Less Like This" buttons for legacy sessions.Tracks last N (≈ 10 – 15) played songs with their:

- Prerequisites:

  - `LASTFM_API_KEY` set in environment/.env.- genres, artists, BPM/mood vectors, skip outcomes.

  - `GeminiApiKeys` (comma-separated) or legacy `GEMINI_API_KEY`.

  - Lavalink/Pomice reachable; resolver logs warn when node access fails.From this derive:

---```python

current_focus_genre = mode(last_genres)

## Outstanding Workcurrent_mood_vector = avg(last_moods)

recent_artists = {…}

The following items remain open after the current implementation pass:```

1. **Pomice smoke tests** – create a lightweight script to validate search/resolve against a test Lavalink node (Phase 3).This defines the **core vibe** window.

2. **Architecture documentation refresh** – update `ARCHITECTURE.md` (or equivalent) with the mood/collaborative flow; tighten this `v2_docs.md` as code evolves (Phase 4).

3. ~~Runbook authoring – add `docs/autoplay_v2.md` covering env setup, quota resets, telemetry opt-out, and standard troubleshooting (Phase 5).~~ ✅ Completed; see `docs/autoplay_v2.md`.---

4. **Unit & async tests** – add pytest coverage for Gemini parsing fallbacks, cache hit paths, collaborative blending, and resolver reuse (Phase 6).

5. **Dataclass audit** – evaluate migrating remaining plain dict responses to typed dataclasses for clarity (Phase 6).### 2️⃣ **Cluster Engine (Genre / Mood embedding space)**

6. **Heuristics sorter v2** – the advanced authenticity filter described in Issue #1 is still pending a dedicated module; current implementation relies on Last.fm candidate quality plus resolver heuristics.

7. **Context planner enhancements** – full context tracker, novelty controller, and mini-arc planning described in the design brief are not implemented yet; current recommender uses a simplified blend.Each track has or can be inferred a **feature vector**:

---```

v = [genre, mood, bpm, energy, valence, popularity, release_age]

## Operational Notes```

- Cache files live under `cache/music/` (`mappings_v2.json`, `enrichment_v2.json`, `parsing_v2.json`, `mood_vectors_v2.json`, `collaborative_embeddings.json`, `telemetry/events.jsonl`).Then:

- `AutoplayEngineV2.get_stats()` surfaces availability, cache counts, collaborative metadata, and telemetry buffer counts for observability commands or health endpoints.

- Telemetry opt-out can be managed per guild/user via `FeedbackManager.set_guild_opt_out()` / `set_user_opt_out()`; anonymisation salt stored at `cache/music/telemetry/salt.txt`.- Cluster songs into **micro-genres / moods** (can be static or cached per enrichment).

- Run `python scripts/cf_train.py --input cache/music/telemetry/events.jsonl --output modules/music/Autoplay_Engine/v2/cache/collaborative_embeddings.json` to refresh collaborative embeddings after collecting telemetry.- Compute **centroid distance** between candidate and `current_mood_vector`.

- Logging:

  - Gemini quota exhaustion and key rotation events log at WARNING.| Distance | Interpretation | Weight |

  - Collaborative scoring summaries and resolver cache hits emit at DEBUG for targeted diagnostics.| ---------- | --------------- | -------------------------------- |

| 0 – 0.25 | Same cluster | +0.4 |

---| 0.25 – 0.5 | Similar cluster | +0.2 |

| 0.5 – 0.8 | Mild novelty | +0.05 |

## Quick Reference| > 0.8 | Hard novelty | –0.1 (unless forced exploration) |

| Component | File | Notes |→ Produces a **cluster_score** that rewards continuity but allows controlled drift.

| --------- | ---- | ----- |

| Orchestrator (public API) | `modules/music/Autoplay_Engine/v2/__init__.py` | Provides `get_lastfm_autoplay_v2(bot)` used by `MusicPlayer`. |---

| Gemini service | `modules/music/Autoplay_Engine/v2/gemini_service.py` | Handles enrichment calls + batching. |

| Cache manager | `modules/music/Autoplay_Engine/v2/cache_manager.py` | TTL-based storage for all enriched artefacts. |### 3️⃣ **Candidate Mixer (Balancing core / discovery / rediscovery)**

| Feedback telemetry | `modules/music/Autoplay_Engine/v2/feedback_manager.py` | Emits anonymised telemetry. |

| Collaborative matrix | `modules/music/Autoplay_Engine/v2/collaborative_matrix.py` | Supplies similarity lookups. |Define proportions per playlist arc (adaptive):

| Recommender | `modules/music/Autoplay_Engine/v2/contextual_recommender.py` | Scores candidates with mood/collab blending. |

| Resolver | `modules/music/Autoplay_Engine/v2/track_resolver.py` | Rebuilds Pomice tracks & persists mappings. || Phase | Core | Similar | Novel | Rediscover |

| Offline CF stub | `scripts/cf_train.py` | Generates collaborative embeddings from telemetry. || ---------------------------- | ---- | ------- | ----- | ---------- |

| Config switch | `modules/music/Autoplay_Engine/config.py` | Selects V1 vs V2 and exposes feedback button support flag. || Stable listening (low skips) | 70 % | 20 % | 10 % | 0 % |

| Rising skips / boredom | 55 % | 30 % | 15 % | 0 % |

This document should be kept in sync with `v2_plan.md` as remaining tasks move toward completion.| End of session / long play | 60 % | 20 % | 10 % | 10 % |

Implemented as weighted sampling among candidate groups.

---

### 4️⃣ **Scoring Formula**

For each candidate:

```python
base_score = (
    0.4 * seed_similarity +
    0.3 * cluster_score +
    0.2 * quality_score +
    0.1 * novelty_bonus
)

feedback_mult = genre_mult * artist_mult * track_mult
diversity_penalty = 1 / (1 + recent_occurrences[artist])

final_score = base_score * feedback_mult * diversity_penalty
```

Then normalize and softmax to probabilities.

---

### 5️⃣ **Novelty Controller**

#### a. **Exploration Rate (dynamic)**

```
novelty_rate = clamp(0.1 + (recent_skip_rate - 0.25) * 0.4, 0.05, 0.25)
```

If users skip more → explore a bit more;
if they stop skipping → stay stable.

#### b. **Exploration Radius**

Only pick candidates within controlled embedding distance ≤ 0.8 (avoid genre leaps).

#### c. **Rediscovery Path**

Every 20 – 30 songs, sample one track from older feedback-negative genres with decayed bias.

---

### 6️⃣ **Anti-Repetition Logic (Improved)**

Instead of binary “already played → block”, use _soft repetition cost_:

```
repetition_penalty = exp(–time_since_last_play / τ)
```

where τ ≈ 6 songs.
Older repeats get forgiven; very recent ones heavily penalized but not completely excluded.

---

### 7️⃣ **Temporal Planning (Mini-Arc)**

Pre-select next 5 songs as a “playlist arc”:

1. 1–3: same cluster (stability)
2. 4: related cluster (variety)
3. 5: mild novelty (exploration)

Update arc dynamically after each feedback event.

---

### 8️⃣ **Feedback Integration**

- Positive plays pull cluster centroid toward that feature space.
- Consecutive dislikes shift centroid away and raise novelty rate.
- Bias decay ensures context re-evaluates after ~15 min.

---

### 9️⃣ **Rediscovery Scheduler**

Every few sessions, test previously disliked genres with lower weight (–0.2 → –0.05).
This mirrors Apple Music’s “retry forgotten vibes” behavior.

---

## ✅ **Summary Table**

| Aspect             | Old Behavior | New Behavior                |
| ------------------ | ------------ | --------------------------- |
| Novelty control    | Random       | Distance-based (controlled) |
| Cluster continuity | Weak         | Strong (mood/genre vectors) |
| Feedback coupling  | Loose        | Real-time adaptive          |
| Repetition logic   | Hard ban     | Exponential decay           |
| Discovery pacing   | None         | Planned arcs                |
| Rediscovery        | None         | Timed re-tests              |
| Stability          | Inconsistent | Smooth, Spotify-like        |

---

## 🧱 **Implementation Plan**

| Step | Module              | Action                                 |
| ---- | ------------------- | -------------------------------------- |
| 1    | `ContextTracker`    | Track recent songs, moods, skips       |
| 2    | `ClusterEngine`     | Maintain mood/genre embeddings         |
| 3    | `CandidateMixer`    | Weighted sampling (core/similar/novel) |
| 4    | `Scoring`           | Apply formula + multipliers            |
| 5    | `NoveltyController` | Dynamic exploration rate               |
| 6    | `AntiRepeat`        | Exponential penalty model              |
| 7    | `ArcPlanner`        | Plan next N songs (optional pre-fetch) |

---

## 📈 **Expected Result**

- 80 – 90 % perceived accuracy (Apple Music-like consistency).
- Seamless genre transitions.
- Discovery feels natural, not random.
- Repetition minimized but not overly strict.
- Algorithm gracefully adapts to skips and taste shifts.

---

Would you like me to now move forward with a **pseudocode implementation of this new “Contextual Arc Recommender”**, showing how it would plug into your existing candidate-scoring and autoplay loop?
That version would be designed to replace your current `_score_and_resolve_candidates` logic cleanly.

Got it — that’s exactly the right distinction.
Let’s lock this in as the **final design direction for Issue #2** before we move to code.

Here’s the corrected and finalized version, fully aligned with your clarification 👇

---

## 🎯 **ISSUE #2 Finalized Solution – Adaptive, Session-Scoped Feedback System (with AFK Handling)**

### 🧩 **Design Goals**

1. **Learn fast:** adjust within 1–3 skips.
2. **Reset fast:** new VC = clean bias state.
3. **Fair & context-aware weighting:**

   - Deafened = definitely AFK → ignored.
   - Muted = passive listener → normal weight.
   - DJ = same as others (no extra boost).

4. **Optional persistence:** DJs can save or restore a tuned “vibe” file (.json).

---

## 🧱 1️⃣ Feedback Scope Layers

| Layer                | Lifetime                                  | Purpose                      |
| -------------------- | ----------------------------------------- | ---------------------------- |
| **Session Bias**     | Active VC only (resets on end/inactivity) | Reflects current group taste |
| **Guild Bias**       | Optional persistent                       | Default personality          |
| **Snapshot (.json)** | Manual import/export                      | Save preferred tuning        |

Bias resolution order: `snapshot → session → guild → global`.

---

## 🧩 2️⃣ Event Feedback Model

| Event                 | Trigger           | ΔBias | Targets        |
| --------------------- | ----------------- | ----- | -------------- |
| Hard Skip (< 15 s)    | Immediate skip    | –0.8  | genre + artist |
| Medium Skip (15–60 s) | Partial play      | –0.4  | genre + artist |
| Late Skip (> 60 s)    | Near end          | –0.2  | genre + artist |
| Finish                | Full play         | +0.6  | genre + artist |
| Replay                | Played again soon | +1.0  | track + genre  |
| 👍 More Like This     | Manual like       | +1.2  | genre + artist |
| 👎 Less Like This     | Manual dislike    | –1.0  | genre + artist |

---

## 🧮 3️⃣ Fast Learning Curve

Exponential damping so early events count most:

```
Δbias = weight * (1 / (1 + e^(–(n–2))))
```

(First skip ≈ 0.88× effect, second ≈ 0.7×, third ≈ 0.5×.)

---

## 🕓 4️⃣ Rapid Decay / Reset Rules

| Condition     | Action                    |
| ------------- | ------------------------- |
| Same session  | Every 10 min → bias × 0.7 |
| Idle > 15 min | Hard reset                |
| VC ended      | Hard reset                |
| Snapshot load | Replace state             |

---

## 👥 5️⃣ Multi-User Weighting Logic

### **Presence-Based Weights**

```
if user.is_deafened:   w_user = 0.0   # true AFK → ignored
else:                  w_user = 1.0   # normal or muted listener
```

(DJs have no extra weight.)

### **Aggregated Bias**

```
guild_bias[genre] = Σ(user_bias[genre] × w_user) / Σ(w_user)
```

→ Only actual listeners affect results; AFK users are excluded.
→ Muted listeners still count — they might just be quietly enjoying.

---

## 🔁 6️⃣ Integration with Scoring

```python
genre_mult  = clamp(1 + genre_bias[genre] * 0.25, 0.3, 1.8)
artist_mult = clamp(1 + artist_bias[artist] * 0.25, 0.3, 1.8)
track_mult  = clamp(1 + candidate_bias[track_id] * 0.25, 0.3, 1.8)

final_score = base_score * genre_mult * artist_mult * track_mult
```

Takes effect instantly after any feedback event.

---

## 💾 7️⃣ Snapshot System

**Export Example**

```json
{
  "snapshot_name": "Evening Pop Vibes",
  "created_at": 1760012345,
  "genre_bias": { "pop": 1.5, "lofi": 0.8 },
  "artist_bias": { "The 1975": 1.4 }
}
```

**Import:** clears current biases and applies snapshot directly.

---

## 🧪 8️⃣ Novelty & Rediscovery

~10 % of recommendations intentionally ignore bias to probe new genres for rediscovery.

---

## ✅ Summary Table

| Aspect       | Old               | New                       |
| ------------ | ----------------- | ------------------------- |
| Adaptation   | Slow (8–12 skips) | Fast (2–3 skips)          |
| Decay        | None or weeks     | 30 % every 10 min + reset |
| Group Change | Carry-over        | Clean per session         |
| AFK Users    | Still counted     | Ignored if deafened       |
| Muted Users  | Excluded          | Normal weight             |
| DJ Influence | High              | Equal to others           |
| Persistence  | Always on         | Optional snapshot         |

---

## 🧱 Implementation Plan

1. **`FeedbackManager` class**

   - Store and update biases (`genre`, `artist`, `track`).
   - Apply event → Δbias → decay logic.
   - Aggregate active (non-deafened) users.

2. **Snapshot Utility**

   - `export_biases(path)` / `import_biases(path)` JSON helpers.

3. **Integration**

   - Hook into skip / finish events.
   - Multiply biases during scoring.
   - Auto-reset when VC closes.

4. **Decay Scheduler**

   - Every 10 min → `decay_biases()`.

---

### ✅ Outcome

- Instantly responsive recommendation behavior.
- Each VC adapts independently to whoever’s actually listening.
- DJs can save / restore custom tastes.
- AFK (deafened) listeners have zero algorithm impact.

---

Would you like me to move forward and produce the **actual `FeedbackManager` Python implementation** (drop-in ready for your `lastfm_autoplay.py`, matching these rules)?

Excellent — this is a clean, long-term optimization plan.
Here’s a full **summary and specification for Issue #1 (Heuristic Sorter & Mapping System)** integrating all the improvements we’ve discussed:

---

## 🎯 **ISSUE #1: Heuristics Sorter + Mapping & Enrichment Optimization**

### 🧩 GOAL

To drastically improve the **quality, speed, and accuracy** of the autoplay recommendation pipeline by:

1. Filtering out spam / unofficial / low-quality uploads early.
2. Reusing cached **YouTube mappings** and **enrichment data** to avoid redundant checks.
3. Using engagement, verification, and contextual heuristics for higher authenticity ranking.
4. Implementing cache expiry logic (180 days for enrichment, 360 days for mappings).

---

## 🧠 OVERVIEW OF THE SYSTEM FLOW

**Old flow:**
Candidate Pool (≈50) → Heuristic sort (basic keyword filters) → Enrichment (Last.fm, Gemini) → Scoring → Resolve (YouTube) → Playback

**New flow (optimized):**

```
Candidate Pool (~50)
   ↓
[Heuristic Sorter v2]
   ├─ Check cache for enrichment (180d)
   ├─ Check cache for mapping (360d)
   ├─ Engagement & authenticity filters
   ├─ Hard-reject spam/duplicates
   ↓
Shortlisted Candidates (~25–30)
   ↓
Fetch enrichment only if missing
   ↓
Score & resolve (with existing bias & novelty system)
```

---

## ⚙️ **COMPONENT A: Mapping Cache System (YouTube Resolver)**

### 🔹 Purpose

Speed up the recommendation and reduce unnecessary heuristic checks by remembering the _best YouTube URL_ for each Last.fm track.

### 🔹 Structure

```json
{
  "artist:title": {
    "youtube_id": "abc123xyz",
    "url": "https://www.youtube.com/watch?v=abc123xyz",
    "timestamp": 1760011899.9878962
  }
}
```

### 🔹 Behavior

- On resolve, if `artist:title` is found in cache **and not expired (<360 days)**:

  - Skip all heuristic + enrichment resolution.
  - Use the cached YouTube URL directly.

- If expired or missing:

  - Run through Heuristic Sorter → resolve to best match → save new mapping with timestamp.

- If the YouTube link fails to load (removed or unavailable):

  - Delete mapping and re-fetch a new one.

### 🔹 Expiry policy

- **360 days (1 year)** since mapping creation.
- Mapping refresh triggered only on playback failure or expiration.

---

## 🧠 **COMPONENT B: Enrichment Cache (Genre, Mood, Metadata)**

### 🔹 Purpose

Avoid redundant API calls to Last.fm or Gemini for genre/mood enrichment that rarely changes.

### 🔹 Structure

```json
{
  "artist:title": {
    "tags": ["electronic", "darkwave"],
    "mood": "energetic",
    "listeners": 125000,
    "fetched_at": 1754000000
  }
}
```

### 🔹 Behavior

- Load from cache first.
- If missing or older than **180 days**, refresh from Last.fm/Gemini.
- Store enrichment in cache immediately after successful fetch.

### 🔹 Expiry policy

- **180 days**, as genre/mood rarely changes.
- If Gemini parsing logic gets updated → optional manual refresh all.

---

## 🧮 **COMPONENT C: Heuristics Sorter v2 (Spam & Authenticity Filtering)**

### 🔹 Core Features

| Category                    | Check                                                                                 | Effect            |
| --------------------------- | ------------------------------------------------------------------------------------- | ----------------- |
| **Mapping Shortcut**        | If mapping exists & valid → skip filtering                                            | ⏩ Fast bypass    |
| **Keyword Filters**         | Uses `GOOD_TITLE_KEYWORDS`, `BAD_TITLE_KEYWORDS` (existing)                           | Base + / -        |
| **Channel Authenticity**    | Verified badge → +0.3                                                                 | Boost             |
|                             | Channel name ≈ artist name → +0.25                                                    | Boost             |
|                             | Contains “vevo”, “topic”, “records” → +0.2                                            | Boost             |
|                             | Contains “fanmade”, “reupload”, “preview”, “milestone”, “subscriber”, “teaser” → -0.4 | Penalty           |
|                             | “Official” but unverified channel → -0.5                                              | Penalty           |
| **Engagement Metrics**      | likes/views < 0.5% → -0.4                                                             | Likely spam       |
|                             | likes/views > 5% → +0.15                                                              | Popular & trusted |
|                             | comments/views < 0.1% → -0.2                                                          | Dead engagement   |
| **Duration Mismatch**       | >25% off from Last.fm duration → -0.2                                                 | Likely remix      |
| **Recent Resolve Failures** | Cached failure → hard reject                                                          | ❌                |
| **Loop/Hour/Compilation**   | Contains “loop”, “hour”, “mix”, “karaoke” → hard reject                               | ❌                |

### 🔹 Scoring Model

```
_base = 0.65
filter_score = clamp( _base + quality_score + Σ(reason_deltas) )
```

- Reject if `< 0.22`
- Shortlist top `25–30` by `(filter_score * 0.6 + quality_score * 0.4)`

### 🔹 Example Outcome

**Case:**
“My Ordinary Life - The Living Tombstone (Official) #subscribermilestone #preview #song”
→ +0.15 (official keyword)
→ -0.8 (milestone + preview)
→ -0.4 (low like ratio)
→ no verification
✅ **Final: rejected (score < 0.2)**

---

## 🔁 **COMPONENT D: Integration With Feedback System (Next Issue)**

- Sorter respects session multipliers (`genre`, `artist`, `user_feedback`).
- If a genre has consecutive skips → its multiplier lowers → fewer candidates make shortlist.
- Skip severity (hard / medium / soft) modifies candidate multiplier, not immediate rejection — ensures balanced adaptation.

---

## 💾 **COMPONENT E: Expiry, Cleanup, and Resilience**

| Cache                         | Max Age            | On Failure       | Refresh Trigger           |
| ----------------------------- | ------------------ | ---------------- | ------------------------- |
| YouTube Mapping               | 360 days           | delete & refetch | playback fail or expiry   |
| Enrichment                    | 180 days           | delete & refetch | on stale or Gemini update |
| Heuristic Cache (filter logs) | 30 days (optional) | rotate out       | debugging only            |

---

## 🧩 **Pipeline Summary**

```text
1. Load Candidate Pool (~50)
2. Check YouTube Mapping Cache
   - If valid → skip to playback
   - Else → run Heuristic Sorter
3. Heuristic Sorter
   - Engagement, authenticity, duration, spam filters
   - Shortlist top 25–30
4. Load Enrichment Cache (180d)
   - Missing → request + store
5. Scoring (diversity, novelty, feedback multipliers)
6. Playback / Cache new mapping (360d)
```

---

## 🧱 **Implementation To-Do**

| Priority    | Task                                                           | Description                             |
| ----------- | -------------------------------------------------------------- | --------------------------------------- |
| 🔹 High     | Implement mapping cache system                                 | Store artist:title → YouTube ID + URL   |
| 🔹 High     | Integrate cache check before heuristics                        | Skip if mapping exists                  |
| 🔹 High     | Expand heuristic sorter with engagement + authenticity filters | As specified above                      |
| 🔹 Medium   | Add cache expiry + cleanup utility                             | Auto delete expired mappings/enrichment |
| 🔹 Medium   | Hook enrichment reuse into sorter                              | Load before calling Gemini              |
| 🔹 Optional | Add filter reason logging                                      | For tuning accuracy                     |
| 🔹 Optional | Metrics counter                                                | Count dropped vs kept for debugging     |

---

## ✅ **Expected Impact**

| Metric                      | Before             | After                 |
| --------------------------- | ------------------ | --------------------- |
| Average processing time     | ~5–7s              | ~1–2s (w/ cache hits) |
| API calls per song          | ~2–3               | ~0–1 (mostly cache)   |
| Spam rate in recs           | 10–15%             | <2%                   |
| Repetition fallback quality | Unreliable         | 90%+ official         |
| Enrichment accuracy         | High but redundant | High + persistent     |

---

Would you like me to now create the **full `heuristics_sorter_v2` Python implementation** that:

- Uses your constants and helpers,
- Integrates cache loading/saving,
- Handles mapping expiry logic (360d), and
- Logs reasons for debug?

That version will be directly droppable into your `lastfm_autoplay.py` — ready for testing.

# Recommendation Engine Overhaul Plan (Consolidated)

This document consolidates all resolved issues and design blueprints for the new generation **Discord Music Bot Recommendation System**. It integrates the three major overhaul issues discussed: **Heuristic Sorter**, **Feedback System**, and **Recommendation / Novelty Engine**.

---

## 🎯 OVERVIEW

The overhaul addresses these goals:

1. Filter out spam and non-official uploads while leveraging caching.
2. Implement fast, session-based feedback learning that adapts per VC.
3. Rebuild the recommendation system to behave like Apple Music / YouTube — gradual novelty, strong vibe consistency, and adaptive exploration.

---

## 🧱 ISSUE #1 — Heuristics Sorter + Mapping & Enrichment Optimization

### **Goal**

Enhance filtering and performance by:

- Using **mapping and enrichment caches** (360d and 180d respectively).
- Adding **engagement-based filters** and **contextual spam detection**.
- Supporting cache refresh on failure and cache pruning.

### **Pipeline**

```
Candidate Pool (~50)
   ↓
Check Mapping Cache (360d)
   ↓
Heuristic Sorter v2 (engagement + verification + keyword filters)
   ↓
Check Enrichment Cache (180d)
   ↓
Score and Select
   ↓
Playback & Update Mapping Cache
```

### **Filters & Checks**

| Category                 | Check                                    | Effect           |
| ------------------------ | ---------------------------------------- | ---------------- |
| **Mapping Shortcut**     | If valid mapping found → skip heuristics | Fast path        |
| **Channel Authenticity** | Verified, matches artist name            | Boost            |
| **Engagement**           | likes/views ratio                        | Boost or penalty |
| **Spam Keywords**        | reupload, milestone, teaser, preview     | Hard reject      |
| **Reaction Detection**   | reaction, review, critique               | Hard reject      |
| **Loop/Compilation**     | loop, hour, karaoke                      | Reject           |
| **Duration Mismatch**    | >25% difference from Last.fm             | Penalty          |

### **Mapping Cache Structure**

```json
{
  "artist:title": {
    "youtube_id": "abc123",
    "url": "https://youtube.com/watch?v=abc123",
    "timestamp": 1760011899.98
  }
}
```

### **Enrichment Cache Structure**

```json
{
  "artist:title": {
    "tags": ["pop", "electronic"],
    "mood": "upbeat",
    "listeners": 120000,
    "fetched_at": 1754000000
  }
}
```

### **Expiry Policy**

| Cache      | TTL      | Refresh Condition            |
| ---------- | -------- | ---------------------------- |
| Mapping    | 360 days | Expired or playback failure  |
| Enrichment | 180 days | Expired or enrichment update |

### **Expected Result**

- Processing latency drops from 5–7s → ~1–2s with cache hits.
- Spam videos reduced to <2%.
- Consistent official track mapping and higher accuracy.

---

## 🧩 ISSUE #2 — Adaptive Session-Based Feedback System

### **Goals**

- Adapt within 1–3 skips.
- Reset bias between sessions (new VC = clean state).
- Weight listeners dynamically (deafened = ignored, muted = normal weight).
- Support exporting and importing feedback snapshots (.json).

### **Bias Layers**

| Layer        | Lifetime            | Purpose                |
| ------------ | ------------------- | ---------------------- |
| Session Bias | Active VC           | Reflects current taste |
| Guild Bias   | Optional persistent | Default vibe           |
| Snapshot     | Manual              | Save preferred tuning  |

### **Feedback Events**

| Event                | ΔBias | Scope          |
| -------------------- | ----- | -------------- |
| Hard Skip (<15s)     | –0.8  | genre + artist |
| Medium Skip (15–60s) | –0.4  | genre + artist |
| Late Skip (>60s)     | –0.2  | genre + artist |
| Finish               | +0.6  | genre + artist |
| Replay               | +1.0  | track + genre  |
| 👍 More Like This    | +1.2  | genre + artist |
| 👎 Less Like This    | –1.0  | genre + artist |

### **Decay / Reset Rules**

- Every 10 min → bias × 0.7.
- Idle > 15 min or VC ended → full reset.

### **Presence Weighting**

```
if user.is_deafened: weight = 0.0
else: weight = 1.0
```

DJs have no special weight.

### **Integration Example**

```python
genre_mult = clamp(1 + genre_bias[genre] * 0.25, 0.3, 1.8)
artist_mult = clamp(1 + artist_bias[artist] * 0.25, 0.3, 1.8)
track_mult = clamp(1 + track_bias[track_id] * 0.25, 0.3, 1.8)
final_score = base_score * genre_mult * artist_mult * track_mult
```

### **Snapshots**

JSON export/import for sharing session tuning.

### **Expected Result**

- Fast adaptation (2–3 skips).
- AFK listeners ignored (deafened users = 0 influence).
- DJs can load/export tuned bias profiles.

---

## 🧠 ISSUE #3 — Contextual Arc Recommender (Novelty Algorithm)

### **Goal**

Emulate Apple Music/YouTube behavior:

- Maintain mood and genre continuity.
- Introduce gradual novelty.
- Plan discovery arcs every few songs.
- Avoid random leaps and wrong clusters.

### **Architecture**

```
RecentContext  →  Cluster Engine  →  Candidate Mixer
```

### **Core Components**

| Component             | Role                                         |
| --------------------- | -------------------------------------------- |
| **RecentContext**     | Tracks last 10–15 songs (genre, mood, skips) |
| **ClusterEngine**     | Embedding vectors for genre/mood clusters    |
| **CandidateMixer**    | Weighted sampling of core/similar/novel sets |
| **NoveltyController** | Adjusts novelty rate based on skip rate      |
| **ArcPlanner**        | Plans mini 5-song arcs (stability → novelty) |
| **AntiRepeatManager** | Exponential penalty for recent repeats       |

### **Candidate Scoring Formula**

```python
base_score = (
  0.4 * seed_similarity +
  0.3 * cluster_score +
  0.2 * quality_score +
  0.1 * novelty_bonus
)
final_score = base_score * feedback_mult * diversity_penalty
```

### **Exploration Rate & Distance**

```
novelty_rate = clamp(0.1 + (recent_skip_rate - 0.25) * 0.4, 0.05, 0.25)
```

Select candidates with distance ≤ 0.8 (prevent sharp jumps).

### **Repetition Model**

```
repetition_penalty = exp(-time_since_last_play / τ)
```

τ ≈ 6 songs.

### **Arc Planning**

| Song # | Phase           | Action               |
| ------ | --------------- | -------------------- |
| 1–3    | Core Cluster    | Maintain vibe        |
| 4      | Similar Cluster | Add variety          |
| 5      | Mild Novelty    | Explore nearby genre |

### **Rediscovery**

Retry lightly disliked genres every 20–30 songs with lower penalty weight.

### **Expected Result**

- Smooth transitions between genres.
- Natural discovery pacing.
- 80–90% user satisfaction consistency.

---

## 🔍 SUMMARY TABLE

| Issue | Focus                | Key Improvement                                |
| ----- | -------------------- | ---------------------------------------------- |
| #1    | Heuristics & Caching | Spam filter, cache reuse, mapping TTL          |
| #2    | Feedback             | Fast learning, AFK handling, reset per session |
| #3    | Recommendation       | Gradual novelty, arcs, rediscovery, stability  |

---

## 🧩 NEXT STEPS

1. Implement `heuristics_sorter_v2` with cache, engagement, and context filters.
2. Implement `FeedbackManager` with event-based bias logic and presence weighting.
3. Implement `Contextual Arc Recommender` (ClusterEngine + ArcPlanner + NoveltyController).
4. Conduct simulated user testing to tune novelty rates and feedback curves.

---

## 📈 EXPECTED OUTCOMES

- Reduced latency (<2s avg).
- Higher perceived accuracy (85–90%).
- Organic mood consistency like Apple Music.
- Faster group adaptation between user sets.
- Seamless handling of AFK and passive listeners.

---

---

## 🔗 Addendum: Collaborative Filtering & ML-based Mood/Energy Detection (V2 Final Plan)

This addendum details the new collaborative filtering (global telemetry) and ML-based mood/energy detection components that will be integrated into `autoplayengine_v2` and the other V2 modules.

### A. Collaborative Filtering (Global)

**Goal:**
Collect anonymized telemetry across guilds (opt-out) to compute item-to-item and genre-to-genre similarity using collaborative signals (co-listen, co-like, skip patterns). This augments content-based embeddings (Last.fm / Gemini) with collaborative signals to improve recommendations, cold-start handling, and cross-user accuracy.

**Telemetry Signals to Record (anonymized):**

- `track_id` / `artist` / `genre`
- `event_type` (play, finish, skip, hard_skip, like, dislike, replay)
- `timestamp`
- Optional: `session_id`, `device` (if available)

User ID and Guild Id will not be recorded as it is not important

**Storage & Privacy:**

- Provide per-guild opt-out and per-user opt-out flags.
- Retention: telemetry retained for 365 days by default; configurable.

**Offline Processing Pipeline:**

1. Batch ingest telemetry (hourly/daily) into a training dataset.
2. Compute co-occurrence matrices: track-track, genre-genre, artist-artist.
3. Train a light-weight matrix factorization model (e.g., implicit ALS) or use item2vec (skip-gram on sequences) to produce collaborative embeddings.
4. Export embeddings to `cache_manager` (sharded files) for fast lookup.

**Online Use:**

- `recommendation_algorithm` blends collaborative similarity with content/cluster similarity. Weighting default: content 0.7, collaborative 0.3 (tunable).
- For cold-start tracks with little metadata, collaborative similarity can dominate.

**Schema for Collaborative Embeddings (stored in cache manager):**

```json
{
  "track_embeddings": {
    "track_id": [0.12, -0.03, ...]
  },
  "genre_embeddings": {
    "lofi": [0.3, 0.1, ...]
  },
  "trained_at": 1760010000
}
```

**Model refresh cadence:**

- Re-train weekly or when telemetry volume increases significantly.
- Support manual retrain trigger.

**A/B testing:**

- Provide a toggle to enable/disable collaborative blending per guild for safe rollout.

### B. ML-based Mood & Energy Detection

**Goal:**
Infer per-track continuous mood/energy/valence features from audio or enriched metadata so cluster embeddings become more accurate than text-only tags. This improves smoothness of mood transitions.

**Approaches:**

1. **Metadata + Acoustic Features Hybrid (preferred initially)**

   - Use Last.fm/Gemini tags + simple extracted audio features (if audio available): tempo (BPM), spectral centroid (brightness), loudness, energy, danceability proxies.
   - Use a small ML model (e.g., XGBoost or lightweight MLP) trained on a labeled dataset (Million Song Dataset or a smaller curated set) to map features → mood vector (energy, valence, tempo category).

2. **End-to-end audio embedding (optional advanced)**

   - Use pre-trained audio models (VGGish, OpenL3) to produce embeddings and fine-tune a small head for mood/energy regression.
   - More accurate but heavier; optional for later.

**Feature extraction (if audio not available):**

- Derive proxies from metadata: tempo tags, genre typical BPM ranges, listener comments, tag co-occurrence.
- When audio is available (e.g., local or via authorized API), run light-weight feature extractor and cache results.

**Inference & Caching:**

- `cache_manager` stores `mood_vector` per track in enrichment cache with confidence score.
- If confidence below threshold, fall back to content-only cluster embedding.

**Schema:**

```json
{
  "artist:title": {
    "mood_vector": [energy, valence, tempo_norm],
    "mood_confidence": 0.78
  }
}
```

**Training & Ops:**

- Use an initial labeled dataset to train the model offline; store model artifact in bot resources.
- Periodic re-training when new data / telemetry suggests drift.

**Runtime constraints:**

- Models must be small (<50MB) and inference fast (≤20ms per track) for real-time scoring.
- If heavy models are required, run as batched offline inference and cache results.

### C. Integration Points (How components tie together)

1. `cache_manager` holds enrichment cache, mapping cache, and collaborative embeddings.
2. `heuristics_sorter` uses enrichment cache & mood confidence to vet candidates early.
3. `feedback_manager` records telemetry events and updates session biases.
4. `recommendation_algorithm` fetches content + collaborative embeddings and mood vectors from `cache_manager` to compute `cluster_score` and `seed_similarity`.
5. `autoplayengine_v2` orchestrates the flow and persists new mappings and enrichment results.

### D. Implementation Roadmap (additional tasks)

- Extend `cache_manager` to store and serve collaborative embeddings and mood vectors.
- Add telemetry writer in `feedback_manager` with opt-out controls.
- Implement offline training pipeline (scripts + scheduler) for collaborative model; expose manual retrain endpoint.
- Implement lightweight mood/energy detector model and batch inference job.
- Integrate collaborative blending into `recommendation_algorithm` scoring.

### E. Privacy & Governance

- Salted hashing of user/guild IDs before storing telemetry.
- Opt-out flags per guild and per user.
- Retention policy and delete-on-request endpoints.

### F. Tuning Defaults

- Content vs Collaborative blend: `content=0.7, collaborative=0.3`.
- Collaborative retrain cadence: weekly.
- Mood confidence threshold: 0.6.
- Telemetry retention: 365 days.

---

This addendum is appended to the master `plan.md`. Once you confirm, I will begin implementing V2 modules in the following order (each delivered as a drop-in module to replace/upgrade V1):

1. `cache_manager` (core caching, mapping, enrichment, collaborative embeddings)
2. `heuristics_sorter` (v2) — uses cache_manager and flags rejects early
3. `feedback_manager` — session-scoped, telemetry writer, snapshot import/export
4. `recommendation_algorithm` — Contextual Arc Recommender + collaborative blending
5. `autoplayengine_v2` — orchestration layer connecting all modules

If that order is acceptable I will start implementing `cache_manager` now and produce the Python module next.
