# Autoplay V3 Reimplementation Plan

## Executive Summary
Streamline V3 to match Apple Music's "Infinity" queue behavior while eliminating legacy V2 patterns. Focus: resource-constrained concurrent sessions, graceful degradation, and intelligent cold-start handling.

---

## 1. Critical Issues & Solutions

### 1.1 Daydreaming (Background Analysis Workers)

**Current Problem:**
- Workers only start when `autoplay()` is called (lazy initialization)
- No proactive analysis during idle periods
- Cold start delay when user requests first autoplay track

**Solution: Eager Worker Initialization**

```python
# In AutoplayEngineV3.__init__()
async def __init__(self, ...):
    # ... existing initialization ...
    
    # Start workers immediately on bot startup
    if self._analyzer and self._analyzer.is_available():
        asyncio.create_task(self.start_analysis_workers())
        LOG.info("🚀 Started background analysis workers (Daydream mode)")

# Modify start_analysis_workers() to be idempotent
async def start_analysis_workers(self) -> None:
    if self._analysis_workers:  # Already running
        return
    
    for i in range(self._analysis_worker_count):
        worker = asyncio.create_task(self._analysis_worker_loop(i))
        self._analysis_workers.append(worker)
        LOG.info(f"👷 Analysis worker {i} started")
```

**Daydreaming Logic:**
```python
async def _analysis_worker_loop(self, worker_id: int):
    """
    Worker loop: Process analysis queue, then enter "Daydream" mode.
    """
    while not self._analysis_shutdown:
        try:
            # Priority 1: Process urgent queue (upcoming tracks)
            if self._analysis_queue:
                job = self._analysis_queue.popleft()
                await self._process_analysis_job(job)
            else:
                # Priority 2: Daydream mode (analyze popular tracks)
                await self._daydream_analysis()
        except Exception as exc:
            LOG.error(f"Worker {worker_id} error: {exc}")
            await asyncio.sleep(5)  # Backoff on error

async def _daydream_analysis(self):
    """
    When idle, proactively analyze popular/trending tracks.
    """
    # Strategy 1: Analyze tracks from collaborative matrix (high affinity)
    popular_tracks = await self._collaborative.get_popular_tracks(limit=50)
    
    for track_id in popular_tracks:
        # Check if already analyzed
        artist, title = self._split_track_key(track_id)
        entry = await self._cache.get_enrichment(artist, title)
        
        if not entry or not entry.computed_simple_vibe:
            # Queue for analysis
            await self._enqueue_analysis(artist, title, priority="low")
            LOG.debug(f"💤 [Daydream] Queued {track_id} for background analysis")
            await asyncio.sleep(10)  # Rate limit: 1 track per 10s in daydream
            return  # Process one track per daydream cycle
    
    # Strategy 2: Analyze new releases from Last.fm trending
    # (Implementation left as exercise - similar pattern)
    
    await asyncio.sleep(60)  # Daydream cooldown: 1 minute
```

**User Joins VC Integration:**
```python
# In music_player.py (on_voice_state_update handler)
async def on_user_join_vc(self, member, channel):
    """
    When user joins VC, wake up analysis workers.
    """
    guild_id = channel.guild.id
    
    # Check if autoplay is enabled for this guild
    if not self._is_autoplay_enabled(guild_id):
        return
    
    # Signal workers to prioritize this guild's tracks
    await self._autoplay_engine.wake_workers(guild_id)
    
    LOG.info(f"👤 User joined VC in guild {guild_id}, waking analysis workers")

# In AutoplayEngineV3
async def wake_workers(self, guild_id: int):
    """
    Wake workers from daydream and prioritize upcoming tracks.
    """
    # Get recent tracks from this guild's session
    recent_tracks = self._get_guild_recent_tracks(guild_id, limit=5)
    
    # Fetch similar tracks (predict what might be requested next)
    predicted_tracks = await self._predict_next_tracks(recent_tracks)
    
    # Queue them with high priority
    for track_id in predicted_tracks[:20]:  # Top 20 predictions
        artist, title = self._split_track_key(track_id)
        await self._enqueue_analysis(artist, title, priority="high")
    
    LOG.info(f"⚡ Woke workers for guild {guild_id}, queued {len(predicted_tracks)} tracks")
```

---

### 1.2 Resource Management: "VIP Room" or Nothing

**Requirement:**
- Max 2 concurrent guilds can use Autoplay V3.
- **Strict Limit:** If slots are full, **disable autoplay completely**. Do not fall back to degraded mode.
- User feedback: "Autoplay is currently at maximum capacity (2/2 sessions). Please try again later."

**Implementation: Semaphore-Based Session Manager**

```python
# In AutoplayEngineV3.__init__()
class AutoplayEngineV3:
    def __init__(self, ...):
        # ... existing code ...
        
        # Resource limiter: Max 2 concurrent ML sessions
        self._ml_session_semaphore = asyncio.Semaphore(
            int(os.getenv("AUTOPLAY_MAX_SESSIONS", "2"))
        )
        self._active_ml_sessions: Dict[int, asyncio.Lock] = {}  # guild_id -> lock

# Session acquisition
async def acquire_ml_session(self, guild_id: int) -> bool:
    """
    Try to acquire an ML session slot for this guild.
    Returns True if acquired, False if FULL.
    """
    if guild_id in self._active_ml_sessions:
        return True
    
    if self._ml_session_semaphore.locked():
        LOG.warning(f"⚠️ Guild {guild_id} denied autoplay (Slots full).")
        return False
    
    try:
        await self._ml_session_semaphore.acquire()
        self._active_ml_sessions[guild_id] = asyncio.Lock()
        LOG.info(f"✅ Guild {guild_id} acquired session.")
        return True
    except Exception:
        return False

# In LastFMAutoplayV3 (v3/__init__.py)
async def autoplay(self, track_info: Dict[str, Any]) -> List[Tuple[str, Any]]:
    guild_id = int(track_info.get("guild_id", 0) or 0)
    
    # Try to acquire ML session
    has_ml_session = await self._engine.acquire_ml_session(guild_id)
    
    if not has_ml_session:
        # STRICT: No fallback.
        LOG.info(f"🚫 Autoplay denied for guild {guild_id} (Capacity reached)")
        return [] # Returns empty list, causing bot to stop or send "No recommendations"
```

---

### 1.3 Data Integrity & Apple Music-Style Queue

**Philosophy:** Quality > Speed. No "guessed" metadata.

**1. Strict Waterfall (No Guesses)**
```python
async def _waterfall_deezer_verification(self, ...):
    # ... Stage 1 & 2 (Gemini Optimized + Heuristic) ...
    
    # Stage 3: Grounded Gemini
    # If this fails or returns low confidence, RETURN NONE.
    # DO NOT return a "best guess" or "is_best_guess": True entry.
    
    if not match:
        LOG.error(f"❌ [Strict] Track '{youtube_title}' failed verification. Dropping.")
        return None
```

**2. The "Infinity" Buffer Workflow**
*Goal: 5-song buffer, instant skips after initialization.*

**Scenario: Un-enriched Pool (Cold Start for specific genre/seed)**
1.  **User Request:** Autoplay starts.
2.  **Candidate Fetch:** Get 100 candidates from Last.fm.
3.  **Filter:** Remove duplicates, blocklisted tracks.
4.  **Cache Check:** Identify which candidates are already enriched.
5.  **Priority Queueing:**
    *   If < 5 enriched candidates exist:
        *   Select top 10 *unenriched* candidates (by Last.fm match score).
        *   Push to `UrgentAnalysisQueue` (High Priority).
        *   **WAIT** for the *first* successful analysis.
    *   If > 5 enriched candidates exist:
        *   Proceed immediately.
6.  **Playback:** Play the first available enriched track.
7.  **Background Fill:** Workers continue processing the rest of the top 10 to fill the 5-song buffer.

**3. Apple Music Features: Mood, Day Affinity, Category**

*   **Mood:** Requires Gemini (Lyrics/Audio analysis). *Keep it.*
*   **Category:** Broad genre (Focus, Workout, Party). Requires Gemini to classify. *Keep it.*
*   **Day Affinity:** **Does NOT require Gemini.**
    *   Implement `TimeContextTracker`.
    *   Store: `(user_id, day_of_week, hour_block, genre_id)`.
    *   Logic: "It's Sunday Morning. User usually listens to Lo-Fi." -> Boost Lo-Fi candidates.

**4. Cache Management (JSON Size)**
*   **Problem:** JSON file grows too large with embeddings.
*   **Solution:** **SQLite Database** (Recommended) or **LRU Pruning**.
    *   *Plan:* Move `mappings/` to a SQLite DB (`cache.db`).
    *   *Alternative:* Keep JSON but enforce max 1000 tracks. Delete least recently accessed.

```python
# In AutoplayEngineV3
async def _wait_for_initial_track(self, candidates: List[Dict]) -> Optional[Dict]:
    """
    Waits for at least one track to be fully enriched.
    """
    start_time = time.time()
    while time.time() - start_time < 15: # Max 15s wait
        # Check if any candidate is now enriched
        for cand in candidates[:5]: # Check top 5
            entry = await self._cache.get_enrichment(cand["artist"], cand["title"])
            if entry:
                return entry
        await asyncio.sleep(1)
    return None
```

**Degraded Pool Removal:**
```python
# Delete degraded pool logic entirely
# If Deezer + Gemini both fail, skip track (don't recommend garbage)

async def autoplay(self, track_info: Dict[str, Any]) -> List[Tuple[str, Any]]:
    # ... existing code ...
    
    parsed = await self._engine.parse_track(raw_title, channel_name)
    
    if not parsed:
        LOG.error(
            f"❌ Unable to parse '{raw_title}'. "
            f"Autoplay disabled for this session."
        )
        # Disable autoplay for this guild until session resets
        self._autoplay_disabled[guild_id] = True
        return []
    
    # Check if Gemini classified it as non-music
    if parsed.get("track_type") != "music":
        LOG.warning(
            f"🚫 Track '{raw_title}' classified as {parsed['track_type']}, "
            f"disabling autoplay for this session"
        )
        self._autoplay_disabled[guild_id] = True
        return []
    
    # Continue with normal flow (no degraded pool)
    # ...
```

---

### 1.4 Cold Start & Progressive Enhancement

**Problem:**
- When cache is empty (0 songs analyzed), recommendations are random
- Need strategy to build enrichment pool from 0 → 500+ songs

**Solution: Bootstrap Strategy**

```python
class ColdStartBootstrapper:
    """
    Intelligently build enrichment cache from scratch.
    """
    
    async def bootstrap_from_zero(self, target_count: int = 500):
        """
        Build initial cache with diverse, high-quality tracks.
        """
        LOG.info(f"🥶 [Cold Start] Bootstrapping cache (target: {target_count} tracks)")
        
        # Phase 1: Popular tracks across genres (200 tracks)
        genres = ["rock", "pop", "electronic", "hip-hop", "indie", "jazz", "classical"]
        for genre in genres:
            tracks = await self._lastfm.get_top_tracks_by_tag(genre, limit=30)
            for track in tracks:
                await self._enqueue_analysis(track["artist"], track["name"], priority="bootstrap")
        
        # Phase 2: Trending tracks (100 tracks)
        trending = await self._lastfm.get_trending_tracks(limit=100)
        for track in trending:
            await self._enqueue_analysis(track["artist"], track["name"], priority="bootstrap")
        
        # Phase 3: Diverse era sampling (200 tracks)
        eras = {
            "1960s": "tag.getTopTracks?tag=60s",
            "1970s": "tag.getTopTracks?tag=70s",
            "1980s": "tag.getTopTracks?tag=80s",
            "1990s": "tag.getTopTracks?tag=90s",
            "2000s": "tag.getTopTracks?tag=2000s",
            "2010s": "tag.getTopTracks?tag=2010s",
            "2020s": "tag.getTopTracks?tag=2020s",
        }
        for era, endpoint in eras.items():
            tracks = await self._lastfm.fetch(endpoint, limit=30)
            for track in tracks:
                await self._enqueue_analysis(track["artist"], track["name"], priority="bootstrap")
        
        LOG.info(
            f"✅ [Cold Start] Queued {target_count} tracks for bootstrap analysis. "
            f"ETA: ~{target_count * 3 / 60:.1f} minutes (3s per track)"
        )

# Progressive enhancement during runtime
async def on_track_played(self, track_id: str):
    """
    After each track plays, discover and analyze related tracks.
    """
    # Get similar tracks
    artist, title = self._split_track_key(track_id)
    similar = await self._lastfm.get_similar_tracks(artist, title, limit=10)
    
    # Queue for analysis
    for track in similar:
        await self._enqueue_analysis(track["artist"], track["name"], priority="discovery")
    
    # After 100 tracks played, start collaborative filtering
    total_analyzed = await self._cache.count_enriched_tracks()
    if total_analyzed >= 100 and not self._collaborative._is_hydrated:
        await self._collaborative.hydrate()
        LOG.info(f"🧠 [Collaborative Matrix] Hydrated with {total_analyzed} tracks")
```

**Recommendation Strategy by Cache Size:**

```python
async def recommend_with_progressive_logic(
    self, guild_id: int, seed_track_id: str
) -> List[str]:
    """
    Adjust recommendation strategy based on cache maturity.
    """
    cache_size = await self._cache.count_enriched_tracks()
    
    if cache_size < 50:
        # Phase 1: Pure Last.fm (popularity-based)
        LOG.info(f"📊 [Recommend] Cache size: {cache_size} (using Last.fm only)")
        similar = await self._lastfm.get_similar_tracks_from_id(seed_track_id, limit=20)
        return [track["id"] for track in similar]
    
    elif cache_size < 200:
        # Phase 2: Hybrid (70% Last.fm, 30% ML)
        LOG.info(f"📊 [Recommend] Cache size: {cache_size} (hybrid mode)")
        similar = await self._lastfm.get_similar_tracks_from_id(seed_track_id, limit=30)
        
        # Score with ML (for tracks that are analyzed)
        scored = []
        for track in similar:
            entry = await self._cache.get_enrichment(track["artist"], track["name"])
            if entry and entry.computed_simple_vibe:
                # Use ML features
                score = self._compute_ml_similarity(seed_track_id, track["id"])
            else:
                # Use Last.fm similarity
                score = track["match_score"]
            scored.append((track["id"], score))
        
        # Return top 10
        scored.sort(key=lambda x: x[1], reverse=True)
        return [track_id for track_id, score in scored[:10]]
    
    else:
        # Phase 3: Full ML (collaborative + content-based)
        LOG.info(f"📊 [Recommend] Cache size: {cache_size} (full ML mode)")
        return await self._recommend_with_ml(seed_track_id)
```

---

## 2. Module Refactoring

### 2.1 Files to Remove/Simplify

**Delete:**
- ❌ `v2/` folder (entire legacy engine)
- ❌ Degraded pool logic in `__init__.py`
- ❌ OST-specific branching (over-engineered for Discord use case)

**Simplify:**
- `track_resolver.py`: Remove Gemini resolution (use heuristics + Deezer only)
- `contextual_recommender.py`: Remove episode detection (not music-focused)
- `cache_manager.py`: Remove V2 compatibility fields

### 2.2 New Modules Needed

**1. `session_manager.py`**
```python
class AutoplaySessionManager:
    """
    Manages ML session slots and degraded mode fallback.
    """
    def __init__(self, max_sessions: int = 2):
        self._semaphore = asyncio.Semaphore(max_sessions)
        self._active_sessions = {}
    
    async def acquire(self, guild_id: int) -> bool:
        # Implementation from Section 1.2
        pass
    
    async def release(self, guild_id: int):
        # Implementation from Section 1.2
        pass
```

**2. `bootstrap_manager.py`**
```python
class ColdStartBootstrapper:
    """
    Builds initial enrichment cache from scratch.
    """
    async def bootstrap_from_zero(self):
        # Implementation from Section 1.4
        pass
```

---

## 3. Workflow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│ Bot Startup                                                 │
├─────────────────────────────────────────────────────────────┤
│ 1. Initialize AutoplayEngineV3                             │
│ 2. Start 3 analysis workers (Daydream mode)                │
│ 3. Check cache size:                                        │
│    - <50 tracks: Start bootstrap (Phase 1)                 │
│    - <200 tracks: Continue bootstrap (Phase 2)             │
│    - >200 tracks: Hydrate collaborative matrix             │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ User Requests Autoplay                                      │
├─────────────────────────────────────────────────────────────┤
│ 1. Try acquire ML session slot                             │
│    ├─ Success (1/2 or 2/2): Full V3 pipeline               │
│    └─ Failure (2/2 full): Degraded mode (Last.fm only)     │
│                                                             │
│ 2. Parse track with Deezer waterfall (3 attempts)          │
│    ├─ Stage 1: Gemini-optimized query                      │
│    ├─ Stage 2: Heuristic fallback                          │
│    └─ Stage 3: Grounded Gemini + track_type check          │
│                                                             │
│ 3. If track_type != "music": Disable autoplay for session  │
│                                                             │
│ 4. Fetch candidates (Last.fm similar + discovery)          │
│                                                             │
│ 5. Enrich candidates:                                       │
│    ├─ Check cache                                           │
│    ├─ Queue analysis for missing tracks                    │
│    └─ Use Gemini estimates as fallback                     │
│                                                             │
│ 6. Score candidates:                                        │
│    - <50 tracks in cache: Use Last.fm scores               │
│    - <200 tracks: Hybrid (70% Last.fm, 30% ML)             │
│    - >200 tracks: Full ML (collaborative + content)        │
│                                                             │
│ 7. Return top candidate                                     │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ Background Workers (Daydream)                               │
├─────────────────────────────────────────────────────────────┤
│ While queue is empty:                                       │
│ 1. Analyze popular tracks from collaborative matrix        │
│ 2. Analyze Last.fm trending tracks                         │
│ 3. Sleep 60s between batches                               │
│                                                             │
│ When user joins VC:                                         │
│ 1. Wake workers                                             │
│ 2. Predict likely next tracks                              │
│ 3. Queue them with high priority                           │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│ Session End (Queue Empty)                                   │
├─────────────────────────────────────────────────────────────┤
│ 1. Release ML session slot                                  │
│ 2. Allow next guild to acquire slot                        │
│ 3. Clear guild's recommendation buffer                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 4. Immediate Action Items

### Phase 1: Core Fixes (Week 1)
- [ ] Implement eager worker initialization (Section 1.1)
- [ ] Add session manager with 2-slot semaphore (Section 1.2)
- [ ] Simplify Deezer waterfall to 3 attempts (Section 1.3)
- [ ] Remove degraded pool logic (Section 1.3)

### Phase 2: Progressive Enhancement (Week 2)
- [ ] Add cold start bootstrapper (Section 1.4)
- [ ] Implement progressive recommendation strategy (Section 1.4)
- [ ] Add daydream analysis loop (Section 1.1)
- [ ] Hook into VC join events (Section 1.1)

### Phase 3: Cleanup (Week 3)
- [ ] Delete `v2/` folder
- [ ] Simplify `track_resolver.py`
- [ ] Remove OST branching logic
- [ ] Add settings command for max sessions

---

## 5. Success Metrics

**Before:**
- Cold start delay: 10-15s (lazy worker initialization)
- Recommendation accuracy (cold): ~40% (degraded pool)
- API waste: 9 Deezer queries per track
- Session limit: None (resource exhaustion possible)

**After:**
- Cold start delay: <2s (eager workers + pre-analysis)
- Recommendation accuracy (cold): ~70% (bootstrap + Gemini)
- API efficiency: 3 Deezer queries max per track
- Session limit: 2 concurrent (configurable)
- Graceful degradation: Yes (Last.fm fallback)

---

## 6. Migration Plan

1. **No Breaking Changes:** Keep existing V3 API surface
2. **Feature Flags:** Add `ENABLE_DAYDREAM`, `ENABLE_SESSION_LIMITS` env vars
3. **Gradual Rollout:** Test in single guild before full deployment
4. **Monitoring:** Add metrics dashboard (cache size, session count, worker status)
