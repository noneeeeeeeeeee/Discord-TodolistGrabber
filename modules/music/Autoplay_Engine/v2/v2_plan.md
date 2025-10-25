# Autoplay Engine V2 Implementation Plan

## Phase 0 · Foundations

- [x] Audit existing V1 Gemini + enrichment flow for reusable behaviors (key rotation, cache TTL, 500/day guardrails).
- [x] Inventory current V2 modules (`cache_manager`, `heuristics_sorter`, `feedback_manager`, `arc_recommender`, `autoplayengine_v2`) and document missing pieces.
- [x] Confirm environment variables (`LASTFM_API_KEY`, `GeminiApiKeys`) are surfaced via `.env` loader and update docs if needed.

## Phase 1 · Gemini Service (priority)

- [x] Design `GeminiService` module with:
  - Key rotation, cooldowns, and shared client pooling.
  - Persistent quota tracker (daily counters persisted under `cache/music/gemini_usage.json`).
  - Async helpers: `parse_track_metadata`, `enrich_tags`, `classify_mood_energy` (no Google Search grounding).
  - Back-pressure queue to respect 500/day budget and throttle per-key usage (configurable burst guard).
  - Structured error taxonomy for rate limits vs invalid payloads.
- [x] Update `CacheManager` to cache Gemini outputs (`ParsingEntry`, `MoodVectorEntry`) with TTL alignment to Issue #1 (180d enrichment).
- [x] Wire `LastFMAutoplayV2` init path to require both Last.fm + Gemini availability; log clear failures and expose `is_available()` accordingly.
- [x] Replace `_parse_track_title` heuristic in V2 with Gemini service call + heuristic fallback, caching parsed results.
- [x] Recreate V1's homemade Gemini enrichment batch queue so non-paid users can group up to ~20 grounding calls, preserving the 500/day limit without official batch APIs.
- ✅ Current status: `GeminiService`, `CacheManager`, and `AutoplayEngineV2.parse_track` now cooperate to store Gemini parses with fallback heuristics and availability reporting.

## Phase 2 · Collaborative Filtering Telemetry

- [x] Extend `FeedbackManager` to emit anonymized play/skip/like events into rolling buffers (per guild & global) respecting opt-outs.
- [x] Introduce `CollaborativeMatrix` helper (lightweight implicit ALS/item2vec loader) with cache hydration + weekly reload hook.
- [x] Blend collaborative similarity inside `_score_and_select` (weight 0.3 default) and expose toggles per guild.
- [x] Persist collaborative stats via `CacheManager` (`collab_embeddings.json`) with version checksum for hot reload.

## Phase 3 · Pomice Track Integration

- [x] Implement `_create_track_obj_from_url` in V2 using `pomice.NodePool.get_node().build_track` (async) with fallback to `get_tracks` when mapping lacks prebuilt data.
- [x] Ensure `_resolve_to_youtube` stores mapping metadata (channel, verified flag, duration) and rehydrates full `pomice.Track` on cache hit.
- [x] Update `music_player.py` enqueue path to expect full Pomice track objects from V2 responses; add defensive logging for node unavailability.
- [ ] Add smoke tests or script to validate search + playback against Lavalink test node.

## Phase 4 · Mood & Arc Enhancements

- [x] Swap `_infer_mood_from_tags` with Gemini-backed classifier (uses `GeminiService.classify_mood_energy`) with cache fallback.
- [x] Integrate mood vectors into Arc Recommender distance metric + novelty planning.
- [ ] Document new behavior in `ARCHITECTURE.md` and update `v2_docs.md` with final summary.

## Phase 5 · QA & Observability

- [x] Add structured logging for Gemini quota usage, collaborative blend weights, and Pomice resolution outcomes.
- [x] Provide `get_stats` expansion to include Gemini availability, quota remaining, and collaborative toggle state.
- [x] Draft runbook in `docs/autoplay_v2.md` covering env setup, quota resets, telemetry opt-out, and troubleshooting steps.

## Phase 6 · Follow-ups

- [x] Implement offline pipeline stubs (`scripts/cf_train.py`) for collaborative embeddings refresh.
- [ ] Add unit-style tests (pytest/async) for Gemini parsing fallback, cache hit paths, and Pomice mapping reuse.
- [ ] Evaluate migrating to typed dataclasses for cache entries and service responses for clarity.
