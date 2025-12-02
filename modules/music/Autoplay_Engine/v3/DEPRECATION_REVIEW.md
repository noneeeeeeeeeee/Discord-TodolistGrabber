# V3 Deprecation Review Document

> **Created**: Auto-generated for review  
> **Purpose**: Identify deprecated features, legacy code, and misalignment with `v3_reimplementation.md` vision  
> **Action Required**: Review each item and confirm before removal/refactoring

---

## Summary

The V3 architecture specifies:

- **Deezer-only** data source (no YouTube audio downloads)
- **4-Factor Enrichment**: Librosa → EfficientAT → Deezer → Gemini
- **Dual Vector**: 5D Vibe (Librosa/MobileNet) + 4D Flow (tempo, loudness, key, mode)
- **Gemini Role**: Track parsing, Deezer query generation, grounding - **NOT vibe estimation**

The following items conflict with this vision and should be reviewed for deprecation.

---

## 🔴 HIGH PRIORITY - Active Code Using Deprecated Patterns

### 1. `gemini_service.py::classify_mood_vector()` (Lines 474-540)

**Status**: DEPRECATED - Still being called  
**Issue**: Gemini guesses vibes from tags/genre/description instead of audio analysis  
**V3 Vision**: Vibes should come from Librosa (`_compute_simple_vibe`) or EfficientAT embeddings

```python
async def classify_mood_vector(self, metadata: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Asks Gemini to guess: energy, valence, tempo, confidence
    # This is the OLD approach before audio analysis was implemented
```

**Called By**:

- `autoplayengine_v3.py::get_mood_vector()` (Line ~2270)

**Recommendation**:

- Mark as `@deprecated` with warning log
- Update `get_mood_vector()` to only use cached `computed_simple_vibe` or trigger audio analysis
- Eventually remove entirely

---

### 2. `autoplayengine_v3.py::get_mood_vector()` (Lines 2230-2330)

**Status**: DEPRECATED - Uses Gemini classification instead of audio analysis  
**Issue**: This method calls `classify_mood_vector()` and synthesizes vibes from Gemini guesses

**V3 Vision**:

- Use `computed_simple_vibe` from Librosa analysis (5D vector)
- Use `computed_embedding` from EfficientAT (512D-2048D)
- Gemini should NOT estimate vibes

**Recommendation**:

- Refactor to return cached audio analysis results only
- If no analysis exists, return `None` or trigger background analysis queue
- Remove Gemini mood estimation path

---

### 3. `cache_manager.py::EnrichmentEntry` - Legacy Synthesis Methods

**Status**: DEPRECATED - Heuristic fallbacks that bypass audio analysis

| Method | Line | Issue |
|--------|------|-------|
| `_resolve_energy_score()` | 154-171 | Maps Gemini `mood_energy` or string labels to float |
| `_resolve_valence_score()` | 173-192 | Parses mood text for valence keywords |
| `_estimate_danceability()` | 194-209 | Uses `mood_tempo` + BPM heuristics |
| `_estimate_acousticness()` | 211-227 | Tag-based genre heuristics |
| `_estimate_instrumentalness()` | 229-247 | Tag-based content type heuristics |

**V3 Vision**: All 5D vibe dimensions come from:

- `_compute_simple_vibe()` in `enriching_service.py` (Librosa spectral analysis)
- NOT from tag/genre/mood text parsing

**Recommendation**:

- Mark all 5 methods as `@deprecated`
- Add comment: "V3 uses computed_simple_vibe from Librosa analysis"
- Remove from `from_dict()` vector synthesis (already done - good!)

---

## 🟡 MEDIUM PRIORITY - Legacy Fields to Phase Out

### 4. `cache_manager.py::EnrichmentEntry` - Legacy Gemini Fields

| Field | Type | Issue |
|-------|------|-------|
| `energy` | `Optional[str]` | String labels ("low"/"medium"/"high") - V3 uses floats |
| `mood_energy` | `Optional[float]` | Gemini guess, replaced by `computed_simple_vibe[0]` |
| `mood_valence` | `Optional[float]` | Gemini guess, replaced by `computed_simple_vibe[1]` |
| `mood_tempo` | `Optional[float]` | Gemini guess, replaced by `computed_tempo` |
| `mood_confidence` | `Optional[float]` | Gemini confidence, no longer used for scoring |
| `activity_affinity` | `Optional[str]` | Never fully implemented in V3 |
| `daypart_affinity` | `Optional[str]` | Timezone issues - already addressed in TIME_BIAS_NOTE |

**Recommendation**:

- Keep for backward compatibility with existing cache
- Add deprecation comments
- Don't populate in new code paths
- Remove from `from_dict()` in next major version

---

### 5. `cache_manager.py::EnrichmentEntry` - Estimated Fields

| Field | Purpose | Status |
|-------|---------|--------|
| `estimated_tempo` | Gemini guess until Librosa runs | Keep as temporary fallback |
| `estimated_loudness` | Gemini guess until Librosa runs | Keep as temporary fallback |
| `estimated_key` | Gemini guess until Librosa runs | Keep as temporary fallback |
| `estimated_mode` | Gemini guess until Librosa runs | Keep as temporary fallback |
| `estimated_simple_vibe` | Gemini 5D guess until analysis | Keep as temporary fallback |

**Status**: These are intentionally temporary estimates. **NOT deprecated** but should be clearly documented.

**Recommendation**:

- Add docstring: "Temporary estimate populated by Gemini, replaced when Librosa/MobileNet analysis completes"
- Ensure `analysis_verified=True` when computed_* fields are populated

---

## 🟢 LOW PRIORITY - Already Marked or Correctly Deprecated

### 6. `contextual_recommender.py::_mood_distance()` (Lines 513-530)

**Status**: Already marked DEPRECATED ✅

```python
def _mood_distance(self, anchor_vec: List[float], candidate_vec: List[float]) -> float:
    """
    DEPRECATED: Legacy Euclidean distance scorer for V2 enrichment.
    """
```

**Recommendation**: Remove in next major cleanup (no longer called)

---

### 7. `gemini_service.py` - Batched Enrichment `simple_vibe_guess`

**Status**: CORRECTLY USED as temporary estimate

The `enrich_tracks_batch()` method returns `simple_vibe_guess` which is stored as `estimated_simple_vibe`. This is the **intended temporary flow** - Gemini provides initial estimates that get replaced by audio analysis.

**No action needed** - this is working as designed.

---

## 📋 Cross-Reference Check: Functions Still Using Deprecated Patterns

### Files Importing/Using `classify_mood_vector`

| File | Usage | Action |
|------|-------|--------|
| `autoplayengine_v3.py` | `get_mood_vector()` calls it | Refactor or remove |

### Files Using `_resolve_energy_score` / synthesis methods

These methods exist but are **NOT called** in the current codebase:

- `from_dict()` was updated to **NOT** synthesize vectors ✅
- No other code calls these synthesis methods directly

**Recommendation**: Safe to mark as deprecated with warning, or remove entirely.

---

## 🔧 Proposed Changes

### Phase 1: Mark Deprecated (Non-Breaking)

1. Add `@deprecated` decorator or warning log to:
   - `gemini_service.py::classify_mood_vector()`
   - `autoplayengine_v3.py::get_mood_vector()` (add deprecation notice in docstring)
   - `EnrichmentEntry` synthesis methods

2. Add deprecation comments to legacy fields:
   - `energy`, `mood_energy`, `mood_valence`, `mood_tempo`, `mood_confidence`
   - `activity_affinity`, `daypart_affinity`

### Phase 2: Refactor (Breaking)

1. Remove `classify_mood_vector()` entirely
2. Refactor `get_mood_vector()` to only return cached `computed_simple_vibe`
3. Remove synthesis methods from `EnrichmentEntry`
4. Remove deprecated fields from `EnrichmentEntry` (requires cache migration)

---

## ✅ Correctly Implemented (No Changes Needed)

| Component | Status |
|-----------|--------|
| `enriching_service.py` | ✅ Librosa + MobileNet implementation correct |
| `deezer_fetch.py` | ✅ Deezer-only, no deprecated features |
| `track_resolver.py` | ✅ Uses Gemini for parsing, not vibes |
| `contextual_recommender.py` | ✅ Uses embeddings/simple_vibe (deprecated method already marked) |
| `novelty_controller.py` | ✅ No deprecated features |
| `context_tracker.py` | ✅ Apple Music features implemented |
| `feedback_manager.py` | ✅ Skip decay working |
| `collaborative_matrix.py` | ✅ Collaboration graph working |

---

## Next Steps

Please review this document and confirm:

1. Which items to mark as deprecated (Phase 1)
2. Which items to remove entirely (Phase 2)
3. Any items that should be kept despite being flagged

Once confirmed, I will apply the deprecation markers and remove any approved items.
