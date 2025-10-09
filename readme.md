# Discord-TodolistGrabber

Modern Discord bot for homework tracking and utility modules with an advanced music subsystem powered by Lavalink.

## Getting Started

1. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
2. Run the bot once so it can generate `.env` and default config files.
3. Populate `.env` with your Discord token, JSON-formatted `GeminiApiKeys` (first key is primary, add more for rotation), and (optionally) Lavalink host credentials. If you keep the defaults, the bot can auto-provision Lavalink locally.
4. Invite the bot to your server and start it.

---

# Bot Technical Details

## Music - Autoplay Engine

### Overview

A compact, budget-aware autoplay workflow that:

- Produces tight, session-coherent recommendations (like Spotify/YouTube “lite”),
- Prevents loops (same artist, same track, or low-quality uploader loops),
- Minimizes expensive Gemini + Google grounding calls (500/day budget),
- Uses print-based logging (`print(...)`) with a `LOG_LEVEL` gate for visibility.

Key signals used: Last.fm tags/similar tracks, cached metadata, skip/finish events (listening duration), quality heuristics for uploads, and limited Gemini enrichment. Spotify and YouTube treat skips/finish and engagement as core signals — use them aggressively. ([Spotify][1])

---

### High-level policies (summary)

- **Cache-first**: metadata cached with TTL (default 90 days). Refresh only for high-value items.
- **Two-tier Gemini usage**: non-grounded parsing (cheap/unlimited) + grounded enrichment (counts to 500/day). Only grounded for _top_ ambiguous candidates.
- **Scoring**: hybrid score (content similarity, artist affinity, session coherence, quality, novelty).
- **Loop prevention**: per-track and per-artist cooldowns + escape-hatch (diversity injection).
- **Low-quality filtering**: detection via bad-keywords + uploader/metadata heuristics + duration checks.
- **Print logging**: `LOG_LEVEL = {0:ERROR,1:INFO,2:DEBUG}`; use `print(...)` only.

---

### Data structures (conceptual)

- `session_history[guild_id]`: deque of last `L=40` resolved tracks with fields `{track_id, artist, timestamp, resolved_source, quality_score}`.
- `cache[track_id]`: `{tags, mood, embedding?, timestamp}` (TTL default 90 days).
- `artist_play_counts[guild_id]`: sliding counts over last `R=12` resolved tracks.
- `grounding_counter_day`: persisted integer, reset daily.
- Tunable params (starting values):
  `M=60` (pool), `N=16` (enrich), `K=6` (resolve), `artist_repeat_limit=3`, `artist_cooldown_cycles=3`, `skip_threshold=0.8`, `finish_threshold=0.9`, `epsilon_base=0.08` _(auto-adjusted between 0.02–0.28 from skip/finish feedback)._

---

### Full workflow (step-by-step)

#### 0. On track start / autoplay trigger

1. Gather seed: `{title, artist, duration, guild_id, timestamp}`.
2. `print(f"[AUTO][INFO] Seed: {artist} - {title}")` (if `LOG_LEVEL >= 1`).

#### 1. Candidate pool build (cheap)

- Query Last.fm: `track.getSimilar`, `artist.getTopTracks`, `tag.getTopTracks` → build raw pool of **M=60** unique candidates.
- Heuristics: remove candidates with blacklist keywords, extreme durations (<30s or >20m).
- `print` candidate count and top-5 naive matches for debugging.

#### 2. Quality prefilter

Calculate `quality_score` (0..1) using:

- uploader health signals (if available: view/like ratio, age),
- title heuristics (bad keywords),
- duration sanity.
  Drop candidates with `quality_score < 0.15`.

#### 3. Enrich **only** top-N (budget-aware)

Sort by raw tag/artist overlap; pick **top N = 16**:

- Use cached metadata when available and fresh.
- If metadata missing/ambiguous:

  - Call non-grounded Gemini parse (cheap).
  - If still ambiguous **and** `grounding_counter_day < 500` **and** candidate in top 3 → call Gemini with Google grounding and increment counter.

- Cache results and persist TTL timestamp.

**Rationale:** limit grounding calls to high-value items to preserve the 500/day quota.

#### 4. Scoring (explicit hybrid)

Compute normalized components:

- `content_sim` (tag/embedding) — weight **0.45** (scaled by recent tag feedback multipliers)
- `artist_affinity` — **0.18** (scaled by session feedback + optional +0.10 additive for recent finishes)
- `session_coherence` (session top tags) — **0.12** (tag multipliers applied)
- `quality_score` — **0.15**
- `novelty_bonus` (epsilon-driven) — **0.10** (`ε` widens/narrows dynamically with skip vs. finish rate)

Apply dynamic modifiers:

- **Immediate candidate multipliers**: 0.20 for hard skips (<20%), 0.50 for medium skips (20–80%), 0.85 for late skips (80–90%), ≥1.20 (scaled up to 1.35) for finishes (≥90%).
- **Session feedback**: tag weights ×0.25/0.60/1.20 and artist affinity ×0.35/0.60/1.10 (+0.10 additive) for hard skip, medium skip, and finish respectively, decaying exponentially over the next 10–20 recommendations.
- **Uploader preference**: canonical channels (Official Artist Channel, VEVO, Topics) and high-engagement uploads receive an extra resolver boost to avoid low-quality reuploads.
- **Artist saturation penalty**: if an artist appears more than `artist_repeat_limit` times in last `R` tracks, subtract 0.5 and engage cooldown cycles.

**Why:** skip/finish signals are powerful predictors of preference — use them as primary feedback. ([Soundcharts][2])

#### 5. Preventing _same-track_ loops (new, explicit)

To prevent immediate repeats of the same track (what you observed):

- Maintain `recent_track_set = {track_id: timestamps}` in `session_history`.
- If `track_id` appears in `recent_track_set` within `track_repeat_window = 90 minutes` (configurable) → treat as duplicate and **apply hard penalty** (e.g., set `final_score = -inf` for that candidate). Do not re-resolve the same track for the session unless explicitly requested by a user.
- Also prevent repeated plays caused by failed resolve re-enqueue: mark failed resolves and avoid retrying the same track for `failed_track_cooldown = 30 minutes`.
- If same track appears as a top candidate due to metadata duplication (many uploads), prefer canonical uploader or highest-quality resolved source; if canonical not resolvable, skip.

This stops the “same track repeats” loop you reported.

#### 6. Resolve playable streams _only_ for top-K

- Attempt to resolve top **K = 6** candidates (concurrency limit 3).
- On resolve failure or low-quality resolved target, mark candidate as failed, move to next.
- If all top-K fail, call **escape_hatch()** (see below).
- `print` resolved candidates and any failures for debugging.

#### 7. Escape hatch (break loops & stuck states)

Trigger when:

- artist_repeat_limit exceeded, OR
- same-track attempted again, OR
- `session_skip_rate > 50%` over last 20 tracks, OR
- too many failed resolves >3.

Actions (priority order):

1. **Inject diversity**: pick a different artist with high tag overlap (same mood/tempo) but different artist.
2. **Epsilon bump**: temporarily set `ε=0.2` for next 2–3 picks to inject exploration.
3. **Fallback**: use Last.fm top tracks for seed artist or global top charts for the session's tags for the next 3 picks.
4. **User fallback**: if all else fails, await user input (but do not block — enqueue safe global track).

`print(f"[AUTO][WARN] Escape hatch triggered: {reason}")`

**Why:** short bursts of exploration and safe fallbacks mimic how major platforms break echo chambers and avoid boredom. ([arXiv][3])

#### 8. Commit & persist

- Append chosen track to `session_history` with `timestamp`, `resolved_source`, `quality_score`.
- Update `artist_play_counts` and any cooldown timers.
- Persist cache periodically (background flush).
- `print(f"[AUTO][INFO] Enqueued: {track} — {artist} (score={score:.2f})").`

### 9. Immediate feedback loop (skip / finish)

On every playback outcome compute `progress_ratio = time_played / duration` and apply the exact policy below:

| Progress band | Outcome label | Immediate candidate multiplier       | Tag weight update                | Artist affinity update                            |
| ------------- | ------------- | ------------------------------------ | -------------------------------- | ------------------------------------------------- |
| `< 20%`       | Hard skip     | × **0.20** for upcoming enqueue      | ×0.25 for ~24 h (decay 0.92/rec) | ×0.35 for ~24 h (decay 0.90/rec)                  |
| `20–80%`      | Medium skip   | × **0.50**                           | ×0.60 for ~12 h (decay 0.94/rec) | ×0.60 for ~12 h (decay 0.93/rec)                  |
| `80–90%`      | Late skip     | × **0.85**                           | —                                | —                                                 |
| `≥ 90%`       | Finish        | ≥ ×**1.20** (scaled to 1.35 at 100%) | ×1.20 for ~12 h (decay 0.94/rec) | ×1.10 multiplier **+0.10** additive bump (decays) |

- **Dynamic exploration**: `ε` increases by +0.018 per skip (bounded to 0.28) and shrinks by −0.012×progress per finish (bounded to 0.02), tightening or widening novelty based on satisfaction.
- **Session personalization**: per-guild, in-memory preference vectors record tag/artist boosts (decay 0.96/day) and contribute ±0.6 multipliers while the current session leans toward those sounds.
- **Metrics-first**: every feedback event is appended to `feedback_events` for offline tuning, grid-searching multipliers, and verifying engagement trends.

**Why:** Early abandonment is a high-confidence negative, while deep completion is a strong positive — values mirror common streaming heuristics.

### 10. Offline tuning checklist

- Export `feedback_events` + recommendation snapshots to CSV for grid-search experiments on weights/decays.
- Plot dynamic epsilon versus rolling skip rate to confirm responsiveness.
- Experiment with opt-in, DJ-weighted tweaks (e.g., give the requesting DJ a small bias) while keeping the baseline preference vectors guild-wide.
- Audit resolver engagement metrics (views/likes) to refine canonical channel boosts.

---

### Low-quality upload detection (practical heuristics)

- Expand bad-keyword blacklist (you already have this).
- Duration extremes (<30s or >20m) flagged.
- If resolver provides uploader metadata: deprioritize channels with abnormally low engagement (low views/likes vs age).
- If many near-identical uploads exist for the same canonical track, prefer canonical or verified uploader or highest engagement.
- Optionally: small audio sanity check (silence/very low loudness) if you can fetch snippet.

These heuristics reduce accidental autoplay of low-quality uploads (the “My Ordinary Life” low-quality slip you saw).

---

### Caching & TTL rules (explicit)

- Track metadata TTL: **90 days** (default). On expiry mark for refresh; refresh only when candidate is in top-N.
- Artist similarity TTL: 7–30 days.
- Persist caches to disk; flush in background.
- Maintain `failed_resolve_map` with cooldowns to avoid retry loops.

---

### Logging (print-based)

Set `LOG_LEVEL = {0:ERROR,1:INFO,2:DEBUG}`. Examples:

```py
if LOG_LEVEL >= 1:
      print(f"[AUTO][INFO] Seed: {artist} - {title}")
if LOG_LEVEL >= 2:
      print(f"[AUTO][DEBUG] Candidate pool (post-filter): {[(c.title,c.score) for c in top_candidates]}")
if LOG_LEVEL >= 1 and escape_hatch_triggered:
      print(f"[AUTO][WARN] Escape hatch: {reason}")
```

---

### Metrics to collect for tuning

- Per-recommendation: `resolved_source`, `quality_score`, `final_score`, `user_action` (skip/finish), `progress_ratio`.
- Session aggregates: `skip_rate`, `avg_listen_duration`, `escape_hatch_count`.
- Operational: `grounding_calls_used_today`, `failed_resolves_count`.

Use metrics to tune `artist_repeat_limit`, `skip penalties`, `epsilon_base` ranges, and grounding policies.

---
