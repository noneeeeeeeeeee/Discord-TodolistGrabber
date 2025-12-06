"""
Test cases for BootstrapManager (V3 Autoplay Engine)

Per v3_reimplementation.md, BootstrapManager handles:
- Scenario A (FIRST_RUN): Initial pool population from charts
- Scenario B (DAYDREAMING): Exploration based on user patterns
- Scenario C (NEW_RELEASES): Monthly new releases check

CRITICAL: Tests must use proper dataclass objects (LastFMTrack, DeezerTrack)
         NOT dictionaries! This caught a production bug.
"""

import asyncio
import sys
import os
from dataclasses import dataclass
from typing import List, Optional
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


# Import the actual dataclasses to ensure type safety
from modules.music.Autoplay_Engine.v3.lastfm_client import LastFMTrack
from modules.music.Autoplay_Engine.v3.deezer_fetch import DeezerTrack


# ============================================================================
# FIXTURE: Create proper dataclass instances
# ============================================================================

def create_lastfm_track(artist: str, title: str, playcount: int = 1000) -> LastFMTrack:
    """Create a LastFMTrack dataclass instance (NOT a dict!)."""
    return LastFMTrack(
        artist=artist,
        title=title,
        playcount=playcount,
        listeners=500,
        mbid=None,
        url=f"https://last.fm/music/{artist}/_/{title}",
        match=0.9
    )


def create_deezer_track(
    track_id: str, 
    artist: str, 
    title: str, 
    preview_url: Optional[str] = "https://preview.deezer.com/test.mp3"
) -> DeezerTrack:
    """Create a DeezerTrack dataclass instance."""
    return DeezerTrack(
        id=track_id,
        artist=artist,
        title=title,
        album="Test Album",
        duration_ms=180000,
        preview_url=preview_url,
        bpm=120.0,
        gain=-8.5,
        explicit=False,
        isrc="TEST12345678",
        genres=["pop"]
    )


# ============================================================================
# TEST: Dataclass Type Validation (Prevent dict/dataclass confusion)
# ============================================================================

def test_lastfm_track_is_dataclass():
    """
    CRITICAL: Ensure LastFMTrack uses attribute access, not dict.get().
    This test exists because we had a production bug treating dataclass as dict.
    """
    track = create_lastfm_track("The Beatles", "Yesterday")
    
    # Correct: attribute access
    assert track.artist == "The Beatles"
    assert track.title == "Yesterday"
    assert isinstance(track.playcount, int)
    
    # Verify it does NOT have dict methods
    assert not hasattr(track, "get"), "LastFMTrack should NOT be a dict!"
    assert not hasattr(track, "keys"), "LastFMTrack should NOT be a dict!"
    
    print("✅ LastFMTrack correctly uses attribute access")


def test_deezer_track_is_dataclass():
    """Ensure DeezerTrack uses attribute access."""
    track = create_deezer_track("123", "Artist", "Title")
    
    # Correct: attribute access
    assert track.id == "123"
    assert track.artist == "Artist"
    assert track.preview_url is not None
    
    # Verify it does NOT have dict methods
    assert not hasattr(track, "get"), "DeezerTrack should NOT be a dict!"
    
    print("✅ DeezerTrack correctly uses attribute access")


# ============================================================================
# TEST: Scenario A - First Run (Last.fm Top Charts → Deezer verification)
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_first_run_uses_dataclass_attributes():
    """
    Test that _scenario_first_run correctly handles LastFMTrack dataclass.
    This is the bug that was caught in production - code used .get() on dataclass.
    """
    from modules.music.Autoplay_Engine.v3.bootstrap_manager import BootstrapManager
    
    # Create mock engine
    mock_engine = MagicMock()
    mock_engine._cache = MagicMock()
    mock_engine._cache.guild_id = 123456789
    mock_engine._cache.get_enrichment.return_value = {}
    mock_engine.verbose_level = 1
    
    # Mock track resolver for batch fallback (Phase 2)
    mock_engine._track_resolver = MagicMock()
    mock_engine._track_resolver.resolve_batch_to_deezer = AsyncMock(return_value={
        "resolved": [],
        "failed": [],
        "stats": {"phase1_resolved": 0, "phase2_resolved": 0, "total_failed": 0}
    })
    
    # Create bootstrap manager
    bootstrap = BootstrapManager(mock_engine)
    
    # Create proper LastFMTrack dataclass objects (NOT dicts!)
    mock_lastfm_tracks = [
        create_lastfm_track("Daft Punk", "Get Lucky"),
        create_lastfm_track("The Weeknd", "Blinding Lights"),
        create_lastfm_track("", ""),  # Empty - should be skipped
    ]
    
    # Create mock Deezer results
    mock_deezer_results = [
        [create_deezer_track("1", "Daft Punk", "Get Lucky")],
        [create_deezer_track("2", "The Weeknd", "Blinding Lights")],
    ]
    
    # Patch LastFMClient and DeezerClient
    with patch('modules.music.Autoplay_Engine.v3.bootstrap_manager.LastFMClient') as MockLastFM:
        with patch('modules.music.Autoplay_Engine.v3.bootstrap_manager.DeezerClient') as MockDeezer:
            # Setup LastFM mock
            mock_lastfm_instance = MagicMock()
            mock_lastfm_instance.__aenter__ = AsyncMock(return_value=mock_lastfm_instance)
            mock_lastfm_instance.__aexit__ = AsyncMock(return_value=None)
            mock_lastfm_instance.get_top_tracks = AsyncMock(return_value=mock_lastfm_tracks)
            MockLastFM.return_value = mock_lastfm_instance
            
            # Setup Deezer mock
            mock_deezer_instance = MagicMock()
            mock_deezer_instance.__aenter__ = AsyncMock(return_value=mock_deezer_instance)
            mock_deezer_instance.__aexit__ = AsyncMock(return_value=None)
            
            # Return different results for each search (now takes single query string)
            search_call_count = 0
            async def mock_search(query):
                nonlocal search_call_count
                if search_call_count < len(mock_deezer_results):
                    result = mock_deezer_results[search_call_count]
                    search_call_count += 1
                    return result
                return []
            
            mock_deezer_instance.search_track = mock_search
            MockDeezer.return_value = mock_deezer_instance
            
            # Execute scenario - this should NOT raise AttributeError
            try:
                result = await bootstrap._scenario_first_run()
                print(f"✅ _scenario_first_run returned {len(result)} tracks")
                assert len(result) == 2, f"Expected 2 tracks, got {len(result)}"
                
                # Verify results are DeezerTrack dataclasses
                for track in result:
                    assert hasattr(track, 'artist'), "Result should be DeezerTrack"
                    assert hasattr(track, 'preview_url'), "Result should have preview_url"
                    assert not hasattr(track, 'get'), "Result should NOT be a dict"
                    
            except AttributeError as e:
                if "'LastFMTrack' object has no attribute 'get'" in str(e):
                    pytest.fail(
                        "BUG DETECTED: Code is treating LastFMTrack dataclass as dict!\n"
                        f"Error: {e}\n"
                        "Fix: Replace track_info.get('artist') with track_info.artist"
                    )
                raise


@pytest.mark.asyncio
async def test_scenario_first_run_handles_empty_lastfm():
    """Test fallback to Deezer charts when Last.fm returns empty."""
    from modules.music.Autoplay_Engine.v3.bootstrap_manager import BootstrapManager
    
    mock_engine = MagicMock()
    mock_engine._cache = MagicMock()
    mock_engine._cache.guild_id = 123456789
    mock_engine._cache.get_enrichment.return_value = {}
    mock_engine.verbose_level = 1
    
    bootstrap = BootstrapManager(mock_engine)
    
    # Mock charts from Deezer (fallback)
    mock_chart_tracks = [
        create_deezer_track("1", "Chart Artist", "Chart Song"),
    ]
    
    with patch('modules.music.Autoplay_Engine.v3.bootstrap_manager.LastFMClient') as MockLastFM:
        with patch('modules.music.Autoplay_Engine.v3.bootstrap_manager.DeezerClient') as MockDeezer:
            # LastFM returns empty
            mock_lastfm = MagicMock()
            mock_lastfm.__aenter__ = AsyncMock(return_value=mock_lastfm)
            mock_lastfm.__aexit__ = AsyncMock(return_value=None)
            mock_lastfm.get_top_tracks = AsyncMock(return_value=[])
            MockLastFM.return_value = mock_lastfm
            
            # Deezer returns charts
            mock_deezer = MagicMock()
            mock_deezer.__aenter__ = AsyncMock(return_value=mock_deezer)
            mock_deezer.__aexit__ = AsyncMock(return_value=None)
            mock_deezer.get_charts = AsyncMock(return_value=mock_chart_tracks)
            MockDeezer.return_value = mock_deezer
            
            result = await bootstrap._scenario_first_run()
            
            # Should have fallen back to Deezer charts
            mock_deezer.get_charts.assert_called_once()
            assert len(result) >= 1
            print("✅ _scenario_first_run correctly falls back to Deezer charts")


# ============================================================================
# TEST: Scenario B - Daydreaming (Similar tracks exploration)
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_daydreaming_uses_dataclass_attributes():
    """
    Test that _scenario_daydreaming correctly handles LastFMTrack dataclass.
    Both get_similar_tracks and get_tag_top_tracks return LastFMTrack.
    """
    from modules.music.Autoplay_Engine.v3.bootstrap_manager import BootstrapManager
    
    mock_engine = MagicMock()
    mock_engine._cache = MagicMock()
    mock_engine._cache.guild_id = 123456789
    mock_engine._cache.get_enrichment.return_value = {}
    mock_engine.verbose_level = 1
    
    # Mock track resolver for batch fallback (Phase 2)
    mock_engine._track_resolver = MagicMock()
    mock_engine._track_resolver.resolve_batch_to_deezer = AsyncMock(return_value={
        "resolved": [],
        "failed": [],
        "stats": {"phase1_resolved": 0, "phase2_resolved": 0, "total_failed": 0}
    })
    
    bootstrap = BootstrapManager(mock_engine)
    
    # Seed tracks for exploration
    bootstrap._get_seed_tracks_for_exploration = AsyncMock(
        return_value=[("Daft Punk", "Digital Love")]
    )
    
    # Similar tracks from Last.fm (proper dataclass objects!)
    mock_similar_tracks = [
        create_lastfm_track("Justice", "D.A.N.C.E."),
        create_lastfm_track("LCD Soundsystem", "Daft Punk Is Playing at My House"),
    ]
    
    # Tag-based tracks
    mock_tag_tracks = [
        create_lastfm_track("Kavinsky", "Nightcall"),
    ]
    
    mock_deezer_results = [
        create_deezer_track("1", "Justice", "D.A.N.C.E."),
        create_deezer_track("2", "LCD Soundsystem", "Daft Punk Is Playing at My House"),
        create_deezer_track("3", "Kavinsky", "Nightcall"),
    ]
    
    with patch('modules.music.Autoplay_Engine.v3.bootstrap_manager.LastFMClient') as MockLastFM:
        with patch('modules.music.Autoplay_Engine.v3.bootstrap_manager.DeezerClient') as MockDeezer:
            # LastFM mock
            mock_lastfm = MagicMock()
            mock_lastfm.__aenter__ = AsyncMock(return_value=mock_lastfm)
            mock_lastfm.__aexit__ = AsyncMock(return_value=None)
            mock_lastfm.get_similar_tracks = AsyncMock(return_value=mock_similar_tracks)
            mock_lastfm.get_tag_top_tracks = AsyncMock(return_value=mock_tag_tracks)
            MockLastFM.return_value = mock_lastfm
            
            # Deezer mock
            mock_deezer = MagicMock()
            mock_deezer.__aenter__ = AsyncMock(return_value=mock_deezer)
            mock_deezer.__aexit__ = AsyncMock(return_value=None)
            
            result_index = 0
            async def mock_search(query):
                nonlocal result_index
                if result_index < len(mock_deezer_results):
                    track = mock_deezer_results[result_index]
                    result_index += 1
                    return [track]
                return []
            
            mock_deezer.search_track = mock_search
            MockDeezer.return_value = mock_deezer
            
            try:
                result = await bootstrap._scenario_daydreaming()
                print(f"✅ _scenario_daydreaming returned {len(result)} tracks")
                assert len(result) >= 1, f"Expected at least 1 track"
                
            except AttributeError as e:
                if "'LastFMTrack' object has no attribute 'get'" in str(e):
                    pytest.fail(
                        "BUG DETECTED: _scenario_daydreaming treating LastFMTrack as dict!\n"
                        f"Error: {e}"
                    )
                raise


# ============================================================================
# TEST: Scenario C - New Releases (Deezer only, no Last.fm)
# ============================================================================

@pytest.mark.asyncio
async def test_scenario_new_releases():
    """Test that _scenario_new_releases works with Deezer."""
    from modules.music.Autoplay_Engine.v3.bootstrap_manager import BootstrapManager
    
    mock_engine = MagicMock()
    mock_engine._cache = MagicMock()
    mock_engine._cache.guild_id = 123456789
    mock_engine._cache.get_enrichment.return_value = {}
    mock_engine.verbose_level = 1
    
    bootstrap = BootstrapManager(mock_engine)
    
    mock_new_releases = [
        create_deezer_track("1", "New Artist", "New Song", "https://preview.deezer.com/1.mp3"),
        create_deezer_track("2", "Another Artist", "Another Song", None),  # No preview
    ]
    
    with patch('modules.music.Autoplay_Engine.v3.bootstrap_manager.DeezerClient') as MockDeezer:
        mock_deezer = MagicMock()
        mock_deezer.__aenter__ = AsyncMock(return_value=mock_deezer)
        mock_deezer.__aexit__ = AsyncMock(return_value=None)
        mock_deezer.get_new_releases = AsyncMock(return_value=mock_new_releases)
        MockDeezer.return_value = mock_deezer
        
        result = await bootstrap._scenario_new_releases()
        
        # Should only include tracks with preview URLs
        assert len(result) == 1, "Should filter out tracks without preview_url"
        assert result[0].preview_url is not None
        print("✅ _scenario_new_releases filters tracks without previews")


# ============================================================================
# TEST: DaydreamScenario Enum
# ============================================================================

def test_daydream_scenario_enum():
    """Test that DaydreamScenario enum exists with correct values."""
    from modules.music.Autoplay_Engine.v3.bootstrap_manager import DaydreamScenario
    
    # Verify all expected scenarios exist
    assert hasattr(DaydreamScenario, 'FIRST_RUN')
    assert hasattr(DaydreamScenario, 'DAYDREAMING')
    assert hasattr(DaydreamScenario, 'NEW_RELEASES')
    assert hasattr(DaydreamScenario, 'IDLE')
    
    print("✅ DaydreamScenario enum has all required values")


# ============================================================================
# RUN TESTS DIRECTLY
# ============================================================================

if __name__ == "__main__":
    print("\n" + "="*70)
    print("BOOTSTRAP MANAGER TEST SUITE")
    print("="*70)
    
    # Run sync tests
    test_lastfm_track_is_dataclass()
    test_deezer_track_is_dataclass()
    test_daydream_scenario_enum()
    
    # Run async tests
    print("\n" + "-"*70)
    print("Running async tests...")
    print("-"*70)
    
    loop = asyncio.get_event_loop()
    
    try:
        loop.run_until_complete(test_scenario_first_run_uses_dataclass_attributes())
    except Exception as e:
        print(f"❌ test_scenario_first_run_uses_dataclass_attributes FAILED: {e}")
    
    try:
        loop.run_until_complete(test_scenario_first_run_handles_empty_lastfm())
    except Exception as e:
        print(f"❌ test_scenario_first_run_handles_empty_lastfm FAILED: {e}")
    
    try:
        loop.run_until_complete(test_scenario_daydreaming_uses_dataclass_attributes())
    except Exception as e:
        print(f"❌ test_scenario_daydreaming_uses_dataclass_attributes FAILED: {e}")
    
    try:
        loop.run_until_complete(test_scenario_new_releases())
    except Exception as e:
        print(f"❌ test_scenario_new_releases FAILED: {e}")
    
    print("\n" + "="*70)
    print("TEST SUITE COMPLETE")
    print("="*70)
