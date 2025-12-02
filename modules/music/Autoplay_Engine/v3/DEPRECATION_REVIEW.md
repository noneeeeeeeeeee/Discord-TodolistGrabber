# V3 Deprecation Review Document

> **Created**: Auto-generated for review  
> **Updated**: Cleanup completed  
> **Purpose**: Track deprecated features removed during V3 Quality > Speed refactor

---

## Summary

The V3 architecture specifies:

- **Deezer-only** data source (no YouTube audio downloads)
- **4-Factor Enrichment**: Librosa → EfficientAT → Deezer → Gemini
- **Dual Vector**: 5D Vibe (Librosa/MobileNet) + 4D Flow (tempo, loudness, key, mode)
- **Gemini Role**: Track parsing, Deezer query generation, cultural context - **NOT vibe estimation**
- **Quality > Speed**: Wait for real Librosa/EfficientAT analysis rather than use inaccurate Gemini estimates

---

## ✅ COMPLETED CLEANUP

### 1. `gemini_service.py::classify_mood_vector()` - REMOVED ✅

**Status**: DELETED  
**Reason**: Gemini should not estimate vibe vectors - Librosa/EfficientAT provides accurate audio analysis

Also removed:
- `_build_mood_prompt()` - No longer needed

---

### 2. `gemini_service.py` - `simple_vibe_guess` from enrichment - REMOVED ✅

**Status**: DELETED  
**Reason**: Quality > Speed - don't use inaccurate Gemini estimates

Changes:
- Removed `simple_vibe_guess` from enrichment prompt
- Removed `simple_vibe_guess` from response parsing
- Kept cultural context fields: `tags`, `moods`, `activity_affinity`, `daypart_affinity`, `emotional_intensity`

---

### 3. `autoplayengine_v3.py::get_mood_vector()` - REFACTORED ✅

**Status**: SIMPLIFIED  
**Changes**: 
- Now only returns cached `computed_simple_vibe` from audio analysis
- No longer calls Gemini to estimate vibes
- Returns `None` if track hasn't been analyzed yet

---

### 4. `cache_manager.py::EnrichmentEntry` - CLEANED UP ✅

**Removed Fields**:
- `energy` (string labels - V3 uses floats)
- `mood_energy`, `mood_valence`, `mood_tempo`, `mood_confidence` (Gemini guesses)
- `estimated_tempo`, `estimated_loudness`, `estimated_key`, `estimated_mode`, `estimated_simple_vibe` (temporary estimates)

**Removed Methods**:
- `_resolve_energy_score()` - Tag-based heuristic
- `_resolve_valence_score()` - Text parsing heuristic
- `_estimate_danceability()` - BPM heuristic
- `_estimate_acousticness()` - Genre heuristic
- `_estimate_instrumentalness()` - Tag heuristic

**Kept Fields** (V3 architecture):
- `tags`, `mood` - Cultural context from Gemini
- `bpm`, `key`, `genres` - Deezer metadata
- `activity_affinity`, `daypart_affinity`, `emotional_intensity` - Gemini cultural context (for future use)
- `computed_*` fields - Real audio analysis from Librosa/EfficientAT
- `analysis_*` fields - Bookkeeping

---

### 5. `contextual_recommender.py::_mood_distance()` - REMOVED ✅

**Status**: DELETED  
**Reason**: Legacy V2 Euclidean scorer, replaced by embedding-based similarity

---

### 6. `__init__.py` - Updated Enrichment Processing ✅

**Status**: CLEANED  
**Changes**: Removed parsing of deprecated fields from batch enrichment

---

### 7. `test_enrichment_service.py` - Updated Tests ✅

**Status**: UPDATED  
**Changes**: Removed deprecated field references, updated to match new EnrichmentEntry schema

---

## ✅ Current V3 Architecture (Quality > Speed)

### Gemini Role (Cultural Context Only)
- Track name parsing from natural language
- Deezer search query generation  
- Cultural context enrichment: `tags`, `mood`, `activity_affinity`, `daypart_affinity`
- **NOT** vibe/vector estimation

### Librosa Role (Audio Structure)
- `computed_tempo` - BPM
- `computed_loudness` - dB
- `computed_key` - Musical key (0-11)
- `computed_mode` - Major (1) / Minor (0)
- `computed_simple_vibe` - 5D vector [energy, valence, danceability, acousticness, brightness]

### EfficientAT Role (Semantic Audio)
- `computed_embedding` - 512D-2048D learned embedding
- `computed_embedding_model` - Model identifier
- `computed_embedding_dim` - Dimension count

### Flow
1. User requests track → Gemini parses track name
2. Gemini generates Deezer query → Deezer returns track + preview URL
3. Gemini enriches cultural context (tags, mood, activity_affinity, daypart_affinity)
4. Background worker downloads preview → Librosa + EfficientAT analyze audio
5. `computed_*` fields populated → Track fully enriched
6. Recommendations use `computed_simple_vibe` / `computed_embedding` for similarity

---

## ✅ Correctly Implemented (No Changes Needed)

| Component | Status |
|-----------|--------|
| `enriching_service.py` | ✅ Librosa + MobileNet implementation correct |
| `deezer_fetch.py` | ✅ Deezer-only, no deprecated features |
| `track_resolver.py` | ✅ Uses Gemini for parsing, not vibes |
| `contextual_recommender.py` | ✅ Uses embeddings/simple_vibe for similarity |
| `novelty_controller.py` | ✅ No deprecated features |
| `context_tracker.py` | ✅ Apple Music features implemented |
| `feedback_manager.py` | ✅ Skip decay working |
| `collaborative_matrix.py` | ✅ Collaboration graph working |
| `gemini_service.py` | ✅ Cultural context only, no vibe estimation |
| `cache_manager.py` | ✅ Clean EnrichmentEntry schema |
| `autoplayengine_v3.py` | ✅ Audio analysis workflow correct |
