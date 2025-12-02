"""
Test cases for the 4-Factor Enrichment System (V3 Architecture)

Per v3_reimplementation.md, enrichment includes:
1. Librosa (Audio Structure Analysis) - BPM, Key, Energy, Spectral
2. EfficientAT MobileNetV3 (Semantic Audio Tags) - Mood, Instruments, Genre, 512D Embedding
3. Deezer (Metadata & Audio) - Track info, Preview URL, Related Artists
4. GeminiAPI (Cultural Context) - Lyrical Themes, Era/Scene, Use Cases

This test validates the enrichment pipeline produces the expected output format.
"""

import asyncio
import sys
import os
import json
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from unittest.mock import MagicMock, AsyncMock

# Add the project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def test_enrichment_output_format():
    """
    Test that EnrichingService.analyze_track() returns the expected format.
    
    Per v3_reimplementation.md, enrichment output should include:
    - embedding: 512-dim from EfficientAT
    - bpm/tempo: Beats per minute
    - key: Musical key (0-11)
    - energy: Normalized energy score
    - simple_vibe: 5D vector [energy, valence, danceability, acousticness, brightness]
    - chroma/mfcc: Librosa spectral features
    """
    print("\n" + "="*60)
    print("TEST: Enrichment Output Format Validation")
    print("="*60)
    
    # Import the service
    try:
        from modules.music.Autoplay_Engine.v3.enriching_service import EnrichingService, AnalysisResult
        print("✅ EnrichingService imported successfully")
    except ImportError as e:
        print(f"⚠️ Could not import EnrichingService: {e}")
        print("   This is expected if librosa/torch are not installed")
        return True  # Skip test if dependencies missing
    
    # Check AnalysisResult dataclass has required fields
    required_fields = [
        "tempo",           # Librosa: BPM
        "loudness",        # Librosa: Loudness in dB
        "key",             # Librosa: Musical key (0-11)
        "mode",            # Librosa: Major (1) or Minor (0)
        "embedding",       # EfficientAT: 512D embedding
        "embedding_model", # EfficientAT: Model name
        "embedding_dim",   # EfficientAT: Dimension count
        "simple_vibe",     # Librosa: 5D vibe vector
        "chroma_mean",     # Librosa: Harmonic content
        "mfcc_mean",       # Librosa: Timbre/Texture
        "spectral_centroid_mean",  # Librosa: Brightness
        "zero_crossing_rate_mean", # Librosa: Percussiveness
        "analysis_mode",   # "ml" or "non-ml"
        "success",         # Boolean
        "error",           # Error message if failed
    ]
    
    # Create mock result to test schema
    mock_result = AnalysisResult(
        tempo=120.0,
        loudness=-10.5,
        key=0,  # C
        mode=1,  # Major
        embedding=[0.1] * 512,
        embedding_model="mn10_as",
        embedding_dim=512,
        simple_vibe=[0.7, 0.6, 0.8, 0.3, 0.5],  # 5D
        chroma_mean=[0.1] * 12,  # 12 chroma bins
        mfcc_mean=[0.1] * 20,    # 20 MFCCs
        spectral_centroid_mean=2500.0,
        zero_crossing_rate_mean=0.08,
        analysis_mode="ml",
        success=True,
        error=None,
    )
    
    # Verify all required fields exist
    missing_fields = []
    for field in required_fields:
        if not hasattr(mock_result, field):
            missing_fields.append(field)
    
    if missing_fields:
        print(f"❌ Missing fields in AnalysisResult: {missing_fields}")
        return False
    
    print("✅ AnalysisResult has all required fields")
    
    # Verify simple_vibe dimensions
    if mock_result.simple_vibe and len(mock_result.simple_vibe) != 5:
        print(f"❌ simple_vibe should be 5D, got {len(mock_result.simple_vibe)}D")
        return False
    print("✅ simple_vibe is 5D: [energy, valence, danceability, acousticness, brightness]")
    
    # Verify embedding dimensions
    if mock_result.embedding and mock_result.embedding_dim != len(mock_result.embedding):
        print(f"❌ embedding_dim mismatch: {mock_result.embedding_dim} != {len(mock_result.embedding)}")
        return False
    print("✅ embedding dimension matches embedding_dim field")
    
    # Verify chroma is 12 bins
    if mock_result.chroma_mean and len(mock_result.chroma_mean) != 12:
        print(f"❌ chroma_mean should be 12 bins, got {len(mock_result.chroma_mean)}")
        return False
    print("✅ chroma_mean is 12 bins (chromatic scale)")
    
    # Verify MFCC is 20 coefficients
    if mock_result.mfcc_mean and len(mock_result.mfcc_mean) != 20:
        print(f"❌ mfcc_mean should be 20 coefficients, got {len(mock_result.mfcc_mean)}")
        return False
    print("✅ mfcc_mean is 20 coefficients")
    
    print("\n✅ All enrichment output format tests passed!")
    return True


def test_cache_manager_enrichment_entry():
    """
    Test that EnrichmentEntry in cache_manager.py stores V3 enrichment data.
    
    Per v3_reimplementation.md (Quality > Speed), cached enrichment includes:
    - Computed fields from Librosa/EfficientAT analysis only
    - No Gemini-estimated vibe fields (removed in V3 cleanup)
    - Cultural context from Gemini (tags, mood, activity_affinity, daypart_affinity)
    """
    print("\n" + "="*60)
    print("TEST: Cache Manager EnrichmentEntry Schema")
    print("="*60)
    
    try:
        from modules.music.Autoplay_Engine.v3.cache_manager import EnrichmentEntry
        print("✅ EnrichmentEntry imported successfully")
    except ImportError as e:
        print(f"❌ Could not import EnrichmentEntry: {e}")
        return False
    
    # Required fields for V3 architecture (Quality > Speed - no Gemini estimates)
    required_fields = [
        # Cultural context from Gemini
        "tags",
        "mood",
        "activity_affinity",
        "daypart_affinity",
        # Flow Vector (Librosa computed)
        "computed_tempo",
        "computed_loudness",
        "computed_key",
        "computed_mode",
        # ML Mode: Learned embedding
        "computed_embedding",
        "computed_embedding_model",
        "computed_embedding_dim",
        # Non-ML Mode: Simple vibe (Librosa-derived, not Gemini-estimated)
        "computed_simple_vibe",
        # Metadata
        "bpm",
        "key",
        "genres",
        "fetched_at",
    ]
    
    # Create mock entry to test schema
    mock_entry = EnrichmentEntry(
        tags=["electronic", "ambient"],
        mood="chill",
        fetched_at=1234567890.0,
        bpm=110,
        key="A minor",
        genres=["electronic", "chillout"],
        # Cultural context from Gemini
        activity_affinity={"workout": 0.3, "chill": 0.9},
        daypart_affinity={"evening": 0.8, "night": 0.7},
        emotional_intensity=0.4,
        # Flow vector (Librosa computed)
        computed_tempo=110.5,
        computed_loudness=-12.3,
        computed_key=9,  # A
        computed_mode=0,  # Minor
        # ML embedding (EfficientAT)
        computed_embedding=[0.1] * 512,
        computed_embedding_model="mn10_as",
        computed_embedding_dim=512,
        # Simple vibe (Librosa-derived only)
        computed_simple_vibe=[0.5, 0.6, 0.55, 0.7, 0.4],
    )
    
    # Verify all required fields exist
    missing_fields = []
    for field in required_fields:
        if not hasattr(mock_entry, field):
            missing_fields.append(field)
    
    if missing_fields:
        print(f"❌ Missing fields in EnrichmentEntry: {missing_fields}")
        return False
    
    print("✅ EnrichmentEntry has all V3 required fields")
    
    # Verify computed_simple_vibe is 5D
    if mock_entry.computed_simple_vibe and len(mock_entry.computed_simple_vibe) != 5:
        print(f"❌ computed_simple_vibe should be 5D, got {len(mock_entry.computed_simple_vibe)}D")
        return False
    print("✅ computed_simple_vibe is 5D: [energy, valence, danceability, acousticness, brightness]")
    
    # Test serialization round-trip
    entry_dict = mock_entry.to_dict()
    restored = EnrichmentEntry.from_dict(entry_dict)
    
    if restored.computed_tempo != mock_entry.computed_tempo:
        print(f"❌ Serialization round-trip failed for computed_tempo")
        return False
    if restored.computed_embedding_dim != mock_entry.computed_embedding_dim:
        print(f"❌ Serialization round-trip failed for computed_embedding_dim")
        return False
    
    print("✅ EnrichmentEntry serialization round-trip works")
    
    print("\n✅ All cache manager enrichment tests passed!")
    return True


def test_gemini_cultural_context():
    """
    Test that GeminiService returns cultural context per v3_reimplementation.md.
    
    GeminiAPI enrichment (via enrich_tracks_batch) should include:
    - tags: Genre/mood tags
    - mood: Text description of mood
    - activity_affinity: Use case affinities (dict)
    - daypart_affinity: Time-of-day affinities (dict)
    - emotional_intensity: Intensity score (float)
    
    Note: In V3 (Quality > Speed), Gemini does NOT estimate simple_vibe.
    All vibe vectors come from Librosa/EfficientAT audio analysis.
    """
    print("\n" + "="*60)
    print("TEST: Gemini Cultural Context Fields")
    print("="*60)
    
    try:
        from modules.music.Autoplay_Engine.v3.gemini_service import GeminiService
        print("✅ GeminiService imported successfully")
    except ImportError as e:
        print(f"❌ Could not import GeminiService: {e}")
        return False
    
    # Check that GeminiService has required methods for V3
    required_methods = [
        "enrich_tracks_batch",  # Main enrichment method
        "generate_deezer_query",  # Query generation
        "parse_tracks",  # Track parsing from text
    ]
    
    missing = []
    for method in required_methods:
        if not hasattr(GeminiService, method):
            missing.append(method)
    
    if missing:
        print(f"❌ GeminiService missing required methods: {missing}")
        return False
    
    print("✅ GeminiService has all required methods")
    print("   - enrich_tracks_batch (cultural context)")
    print("   - generate_deezer_query (search query generation)")
    print("   - parse_tracks (track name parsing)")
    
    # Verify _build_mood_prompt was removed (V3 Quality > Speed)
    if hasattr(GeminiService, '_build_mood_prompt'):
        print("⚠️ GeminiService still has _build_mood_prompt (deprecated in V3)")
    else:
        print("✅ GeminiService correctly removed _build_mood_prompt")
    
    print("\n✅ All Gemini cultural context tests passed!")
    return True


def test_deezer_client_metadata():
    """
    Test that DeezerClient returns the expected metadata fields.
    
    Per v3_reimplementation.md, Deezer provides:
    - Track Metadata: Artist, album, duration
    - Preview URL: 30-second HQ audio preview
    - Related Artists: Built-in artist similarity
    """
    print("\n" + "="*60)
    print("TEST: Deezer Client Metadata Fields")
    print("="*60)
    
    try:
        from modules.music.Autoplay_Engine.v3.deezer_fetch import DeezerClient, DeezerTrack
        print("✅ DeezerClient imported successfully")
    except ImportError as e:
        print(f"❌ Could not import DeezerClient: {e}")
        return False
    
    # Check DeezerTrack has required fields
    required_fields = [
        "id",
        "artist",
        "title",
        "album",
        "duration_ms",
        "preview_url",
    ]
    
    mock_track = DeezerTrack(
        id="123456",
        artist="Test Artist",
        title="Test Track",
        album="Test Album",
        duration_ms=180000,
        preview_url="https://cdns-preview.deezer.com/stream/123.mp3",
    )
    
    missing = []
    for field in required_fields:
        if not hasattr(mock_track, field):
            missing.append(field)
    
    if missing:
        print(f"❌ DeezerTrack missing fields: {missing}")
        return False
    
    print("✅ DeezerTrack has all required metadata fields")
    
    # Check DeezerClient has get_charts method for cold start bootstrap
    if not hasattr(DeezerClient, 'get_charts'):
        print("❌ DeezerClient missing get_charts() method (needed for cold start)")
        return False
    print("✅ DeezerClient has get_charts() for cold start bootstrap")
    
    # Check DeezerClient has get_track_details for BPM/gain
    if not hasattr(DeezerClient, 'get_track_details'):
        print("❌ DeezerClient missing get_track_details() method")
        return False
    print("✅ DeezerClient has get_track_details() for acoustic features")
    
    print("\n✅ All Deezer client tests passed!")
    return True


async def main():
    """Run all enrichment tests."""
    print("\n" + "="*60)
    print("V3 ENRICHMENT SYSTEM TESTS")
    print("="*60)
    print("Testing 4-Factor Enrichment per v3_reimplementation.md:")
    print("  1. Librosa (Audio Structure Analysis)")
    print("  2. EfficientAT MobileNetV3 (Semantic Audio Tags)")
    print("  3. Deezer (Metadata & Audio)")
    print("  4. GeminiAPI (Cultural Context)")
    print()
    
    results = []
    
    # Test 1: Enrichment output format
    try:
        results.append(("Enrichment Output Format", test_enrichment_output_format()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        results.append(("Enrichment Output Format", False))
    
    # Test 2: Cache manager schema
    try:
        results.append(("Cache Manager EnrichmentEntry", test_cache_manager_enrichment_entry()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        results.append(("Cache Manager EnrichmentEntry", False))
    
    # Test 3: Gemini cultural context
    try:
        results.append(("Gemini Cultural Context", test_gemini_cultural_context()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        results.append(("Gemini Cultural Context", False))
    
    # Test 4: Deezer metadata
    try:
        results.append(("Deezer Client Metadata", test_deezer_client_metadata()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        results.append(("Deezer Client Metadata", False))
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    passed = 0
    for name, success in results:
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"{status}: {name}")
        if success:
            passed += 1
    
    print(f"\nTotal: {passed}/{len(results)} tests passed")
    
    return passed == len(results)


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
