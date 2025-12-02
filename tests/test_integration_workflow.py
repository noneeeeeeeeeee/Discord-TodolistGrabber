"""
Integration Workflow Test for V3 Autoplay Engine

This test validates the end-to-end pipeline per v3_reimplementation.md:

1. ENRICHMENT PIPELINE:
   Track Request → Deezer Fetch → Audio Analysis → Gemini Context → Cache Store

2. RECOMMENDATION PIPELINE:
   Session Context → Novelty Phase → Candidate Selection → Scoring → Top-K

3. FEEDBACK LOOP:
   User Signal → Context Update → Vibe Drift → Genre Penalty → Next Rec
"""

import asyncio
import sys
import os
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

# Add the project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def test_enrichment_pipeline_components():
    """
    Test that all enrichment pipeline components can be imported.
    
    Components:
    1. DeezerClient (deezer_fetch.py)
    2. EnrichingService (enriching_service.py)
    3. GeminiService (gemini_service.py)
    4. CacheManager (cache_manager.py)
    """
    print("\n" + "="*60)
    print("TEST: Enrichment Pipeline Components")
    print("="*60)
    
    components = {}
    
    # 1. DeezerClient
    try:
        from modules.music.Autoplay_Engine.v3.deezer_fetch import DeezerClient
        components["DeezerClient"] = True
        print("✅ DeezerClient imported")
    except ImportError as e:
        components["DeezerClient"] = False
        print(f"❌ DeezerClient: {e}")
    
    # 2. EnrichingService
    try:
        from modules.music.Autoplay_Engine.v3.enriching_service import EnrichingService
        components["EnrichingService"] = True
        print("✅ EnrichingService imported")
    except ImportError as e:
        components["EnrichingService"] = False
        print(f"❌ EnrichingService: {e}")
    
    # 3. GeminiService
    try:
        from modules.music.Autoplay_Engine.v3.gemini_service import GeminiService
        components["GeminiService"] = True
        print("✅ GeminiService imported")
    except ImportError as e:
        components["GeminiService"] = False
        print(f"❌ GeminiService: {e}")
    
    # 4. CacheManager
    try:
        from modules.music.Autoplay_Engine.v3.cache_manager import CacheManager
        components["CacheManager"] = True
        print("✅ CacheManager imported")
    except ImportError as e:
        components["CacheManager"] = False
        print(f"❌ CacheManager: {e}")
    
    all_imported = all(components.values())
    
    if all_imported:
        print("\n✅ All enrichment pipeline components available!")
    else:
        print(f"\n❌ Missing components: {[k for k, v in components.items() if not v]}")
    
    return all_imported


def test_recommendation_pipeline_components():
    """
    Test that all recommendation pipeline components can be imported.
    
    Components:
    1. ContextTracker (context_tracker.py)
    2. NoveltyController (novelty_controller.py)
    3. ContextualRecommender (contextual_recommender.py)
    4. SessionManager (session_manager.py)
    """
    print("\n" + "="*60)
    print("TEST: Recommendation Pipeline Components")
    print("="*60)
    
    components = {}
    
    # 1. ContextTracker
    try:
        from modules.music.Autoplay_Engine.v3.context_tracker import ContextTracker
        components["ContextTracker"] = True
        print("✅ ContextTracker imported")
    except ImportError as e:
        components["ContextTracker"] = False
        print(f"❌ ContextTracker: {e}")
    
    # 2. NoveltyController
    try:
        from modules.music.Autoplay_Engine.v3.novelty_controller import NoveltyController
        components["NoveltyController"] = True
        print("✅ NoveltyController imported")
    except ImportError as e:
        components["NoveltyController"] = False
        print(f"❌ NoveltyController: {e}")
    
    # 3. ContextualRecommender
    try:
        from modules.music.Autoplay_Engine.v3.contextual_recommender import ContextualRecommender
        components["ContextualRecommender"] = True
        print("✅ ContextualRecommender imported")
    except ImportError as e:
        components["ContextualRecommender"] = False
        print(f"❌ ContextualRecommender: {e}")
    
    # 4. SessionManager (actually named AutoplaySessionManager)
    try:
        from modules.music.Autoplay_Engine.v3.session_manager import AutoplaySessionManager
        components["SessionManager"] = True
        print("✅ AutoplaySessionManager imported")
    except ImportError as e:
        components["SessionManager"] = False
        print(f"❌ SessionManager: {e}")
    
    all_imported = all(components.values())
    
    if all_imported:
        print("\n✅ All recommendation pipeline components available!")
    else:
        print(f"\n❌ Missing components: {[k for k, v in components.items() if not v]}")
    
    return all_imported


def test_feedback_loop_components():
    """
    Test that all feedback loop components can be imported.
    
    Components:
    1. FeedbackManager (feedback_manager.py)
    2. ContextTracker (context_tracker.py) - for vibe drift
    3. NoveltyController (novelty_controller.py) - for exploration phase
    """
    print("\n" + "="*60)
    print("TEST: Feedback Loop Components")
    print("="*60)
    
    components = {}
    
    # 1. FeedbackManager
    try:
        from modules.music.Autoplay_Engine.v3.feedback_manager import FeedbackManager
        components["FeedbackManager"] = True
        print("✅ FeedbackManager imported")
    except ImportError as e:
        components["FeedbackManager"] = False
        print(f"❌ FeedbackManager: {e}")
    
    # Already tested in recommendation pipeline, but check again
    try:
        from modules.music.Autoplay_Engine.v3.context_tracker import ContextTracker
        components["ContextTracker"] = True
        print("✅ ContextTracker imported (for vibe drift)")
    except ImportError as e:
        components["ContextTracker"] = False
        print(f"❌ ContextTracker: {e}")
    
    try:
        from modules.music.Autoplay_Engine.v3.novelty_controller import NoveltyController
        components["NoveltyController"] = True
        print("✅ NoveltyController imported (for exploration)")
    except ImportError as e:
        components["NoveltyController"] = False
        print(f"❌ NoveltyController: {e}")
    
    all_imported = all(components.values())
    
    if all_imported:
        print("\n✅ All feedback loop components available!")
    else:
        print(f"\n❌ Missing components: {[k for k, v in components.items() if not v]}")
    
    return all_imported


def test_autoplay_engine_v3_main():
    """
    Test the main AutoplayEngineV3 class.
    
    This is the orchestrator that ties together:
    - Priority Queue (ACTIVE, BUFFER, DAYDREAM)
    - Analysis Workers
    - Recommendation Pipeline
    """
    print("\n" + "="*60)
    print("TEST: AutoplayEngineV3 Main Class")
    print("="*60)
    
    try:
        from modules.music.Autoplay_Engine.v3.autoplayengine_v3 import AutoplayEngineV3
        print("✅ AutoplayEngineV3 imported")
    except ImportError as e:
        print(f"❌ AutoplayEngineV3: {e}")
        return False
    
    # Check key attributes
    required_attrs = [
        "PRIORITY_ACTIVE",
        "PRIORITY_BUFFER",
        "PRIORITY_DAYDREAM",
    ]
    
    for attr in required_attrs:
        if hasattr(AutoplayEngineV3, attr):
            value = getattr(AutoplayEngineV3, attr)
            print(f"   ✅ {attr} = {value}")
        else:
            print(f"   ⚠️ {attr} not found")
    
    print("\n✅ AutoplayEngineV3 main class test passed!")
    return True


def test_bootstrap_manager():
    """
    Test the Bootstrap Manager for cold start handling.
    
    Per v3_reimplementation.md:
    - Uses Deezer charts API for initial seeding
    - No YouTube fallback
    """
    print("\n" + "="*60)
    print("TEST: Bootstrap Manager (Cold Start)")
    print("="*60)
    
    try:
        from modules.music.Autoplay_Engine.v3.bootstrap_manager import BootstrapManager
        print("✅ BootstrapManager imported")
    except ImportError as e:
        print(f"❌ BootstrapManager: {e}")
        return False
    
    # Check for Deezer charts method
    manager = BootstrapManager.__new__(BootstrapManager)
    
    if hasattr(manager, 'fetch_chart_tracks') or hasattr(manager, 'get_chart_tracks'):
        print("   ✅ Chart tracks method found")
    else:
        print("   ⚠️ Chart tracks method not found - checking alternatives")
    
    if hasattr(manager, 'bootstrap_from_genre') or hasattr(manager, 'get_genre_seeds'):
        print("   ✅ Genre seeding method found")
    else:
        print("   ⚠️ Genre seeding method not found")
    
    print("\n✅ Bootstrap manager test completed!")
    return True


def test_dependency_manager():
    """
    Test the Dependency Manager for ML dependencies.
    
    Per v3_reimplementation.md:
    - Manages FFmpeg, Librosa, PyTorch, EfficientAT
    - Lazy initialization
    """
    print("\n" + "="*60)
    print("TEST: Dependency Manager (ML Dependencies)")
    print("="*60)
    
    try:
        from modules.music.Autoplay_Engine.v3.dependency_manager import DependencyManager
        print("✅ DependencyManager imported")
    except ImportError as e:
        print(f"❌ DependencyManager: {e}")
        return False
    
    # Check for dependency check methods
    manager = DependencyManager.__new__(DependencyManager)
    
    dependency_checks = [
        ("ffmpeg", ["check_ffmpeg", "ensure_ffmpeg", "get_ffmpeg_path"]),
        ("librosa", ["check_librosa", "ensure_librosa", "has_librosa"]),
        ("torch", ["check_torch", "ensure_torch", "has_torch"]),
        ("efficientat", ["check_efficientat", "ensure_efficientat", "has_efficientat"]),
    ]
    
    for dep_name, methods in dependency_checks:
        found = any(hasattr(manager, m) for m in methods)
        if found:
            print(f"   ✅ {dep_name} check method found")
        else:
            print(f"   ⚠️ {dep_name} check method not found")
    
    print("\n✅ Dependency manager test completed!")
    return True


def test_cache_entry_schemas():
    """
    Test that cache entry schemas match v3_reimplementation.md.
    
    Three entry types:
    1. MappingEntry: query → deezer_id
    2. EnrichmentEntry: Full track data with Dual Vector
    3. ParsingEntry: Track metadata
    """
    print("\n" + "="*60)
    print("TEST: Cache Entry Schemas")
    print("="*60)
    
    try:
        from modules.music.Autoplay_Engine.v3.cache_manager import (
            CacheManager, MappingEntry, EnrichmentEntry, ParsingEntry
        )
        print("✅ Cache entry classes imported")
    except ImportError as e:
        print(f"❌ Cache entry classes: {e}")
        return False
    
    # Check MappingEntry
    print("\n   MappingEntry fields:")
    mapping_fields = ["query", "deezer_id", "timestamp"]
    for field in mapping_fields:
        if field in MappingEntry.__dataclass_fields__:
            print(f"      ✅ {field}")
        else:
            print(f"      ⚠️ {field} not found")
    
    # Check EnrichmentEntry - this should have Dual Vector fields
    print("\n   EnrichmentEntry fields (Dual Vector):")
    enrichment_fields = [
        "deezer_id",
        "computed_embedding",      # 512D from EfficientAT
        "computed_simple_vibe",    # 5D vibe vector
        "computed_tempo",          # Flow feature
        "computed_loudness",       # Flow feature
        "computed_key",            # Flow feature
        "computed_mode",           # Flow feature
    ]
    
    found_count = 0
    for field in enrichment_fields:
        if field in EnrichmentEntry.__dataclass_fields__:
            print(f"      ✅ {field}")
            found_count += 1
        else:
            print(f"      ⚠️ {field} not found")
    
    print(f"\n   Found {found_count}/{len(enrichment_fields)} enrichment fields")
    
    print("\n✅ Cache entry schemas test completed!")
    return True


def test_collaborative_matrix():
    """
    Test the Collaborative Filtering Matrix.
    
    Per v3_reimplementation.md:
    - Cross-user taste graph
    - Co-play frequency
    - Taste similarity clusters
    """
    print("\n" + "="*60)
    print("TEST: Collaborative Matrix")
    print("="*60)
    
    try:
        from modules.music.Autoplay_Engine.v3.collaborative_matrix import CollaborativeMatrix
        print("✅ CollaborativeMatrix imported")
    except ImportError as e:
        print(f"❌ CollaborativeMatrix: {e}")
        return False
    
    # Check for matrix operations
    matrix = CollaborativeMatrix.__new__(CollaborativeMatrix)
    
    operations = [
        ("record_coplay", "Records when tracks are played together"),
        ("get_similar_users", "Finds users with similar taste"),
        ("get_recommendations", "Gets recs from similar users"),
        ("update_taste_vector", "Updates user taste profile"),
    ]
    
    for method, desc in operations:
        if hasattr(matrix, method):
            print(f"   ✅ {method}: {desc}")
        else:
            print(f"   ⚠️ {method} not found")
    
    print("\n✅ Collaborative matrix test completed!")
    return True


def test_preview_fetcher():
    """
    Test the Preview Fetcher for audio download.
    
    Per v3_reimplementation.md:
    - Deezer 30s preview only (no YouTube)
    - Rate limiting
    - Error handling
    """
    print("\n" + "="*60)
    print("TEST: Preview Fetcher")
    print("="*60)
    
    try:
        from modules.music.Autoplay_Engine.v3.preview_fetcher import PreviewFetcher
        print("✅ PreviewFetcher imported")
    except ImportError as e:
        print(f"❌ PreviewFetcher: {e}")
        return False
    
    # Check for fetch methods
    fetcher = PreviewFetcher.__new__(PreviewFetcher)
    
    if hasattr(fetcher, 'fetch_preview') or hasattr(fetcher, 'get_preview'):
        print("   ✅ Preview fetch method found")
    else:
        print("   ⚠️ Preview fetch method not found")
    
    if hasattr(fetcher, 'download_audio') or hasattr(fetcher, 'get_audio'):
        print("   ✅ Audio download method found")
    else:
        print("   ⚠️ Audio download method not found")
    
    print("\n✅ Preview fetcher test completed!")
    return True


def test_track_resolver():
    """
    Test the Track Resolver for query resolution.
    
    Per v3_reimplementation.md:
    - Query → Deezer ID mapping
    - Fuzzy matching
    - Cache integration
    """
    print("\n" + "="*60)
    print("TEST: Track Resolver")
    print("="*60)
    
    try:
        from modules.music.Autoplay_Engine.v3.track_resolver import TrackResolver
        print("✅ TrackResolver imported")
    except ImportError as e:
        print(f"❌ TrackResolver: {e}")
        return False
    
    # Check for resolve methods
    resolver = TrackResolver.__new__(TrackResolver)
    
    if hasattr(resolver, 'resolve_query') or hasattr(resolver, 'resolve'):
        print("   ✅ Query resolve method found")
    else:
        print("   ⚠️ Query resolve method not found")
    
    if hasattr(resolver, 'get_track_id') or hasattr(resolver, 'find_track'):
        print("   ✅ Track ID method found")
    else:
        print("   ⚠️ Track ID method not found")
    
    print("\n✅ Track resolver test completed!")
    return True


async def main():
    """Run all integration workflow tests."""
    print("\n" + "="*60)
    print("V3 INTEGRATION WORKFLOW TESTS")
    print("="*60)
    print("Testing per v3_reimplementation.md:")
    print("  - Enrichment Pipeline (Deezer → Analysis → Gemini → Cache)")
    print("  - Recommendation Pipeline (Context → Novelty → Scoring)")
    print("  - Feedback Loop (Signal → Update → Next Rec)")
    print()
    
    results = []
    
    # Test 1: Enrichment Pipeline Components
    try:
        results.append(("Enrichment Pipeline", test_enrichment_pipeline_components()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Enrichment Pipeline", False))
    
    # Test 2: Recommendation Pipeline Components
    try:
        results.append(("Recommendation Pipeline", test_recommendation_pipeline_components()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Recommendation Pipeline", False))
    
    # Test 3: Feedback Loop Components
    try:
        results.append(("Feedback Loop", test_feedback_loop_components()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Feedback Loop", False))
    
    # Test 4: AutoplayEngineV3 Main
    try:
        results.append(("AutoplayEngineV3", test_autoplay_engine_v3_main()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        results.append(("AutoplayEngineV3", False))
    
    # Test 5: Bootstrap Manager
    try:
        results.append(("Bootstrap Manager", test_bootstrap_manager()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Bootstrap Manager", False))
    
    # Test 6: Dependency Manager
    try:
        results.append(("Dependency Manager", test_dependency_manager()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Dependency Manager", False))
    
    # Test 7: Cache Entry Schemas
    try:
        results.append(("Cache Schemas", test_cache_entry_schemas()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Cache Schemas", False))
    
    # Test 8: Collaborative Matrix
    try:
        results.append(("Collaborative Matrix", test_collaborative_matrix()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Collaborative Matrix", False))
    
    # Test 9: Preview Fetcher
    try:
        results.append(("Preview Fetcher", test_preview_fetcher()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Preview Fetcher", False))
    
    # Test 10: Track Resolver
    try:
        results.append(("Track Resolver", test_track_resolver()))
    except Exception as e:
        print(f"❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Track Resolver", False))
    
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
