"""
Tests for V3 Autoplay Engine Song Analyzer Module

Tests for 3-layer analysis pipeline (Librosa + EfficientAT + Gemini).
Updated to match actual implementation API.
"""

import pytest
import asyncio
import sys
import os
import tempfile
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.song_analyzer import SongAnalyzer, AnalysisTask
from modules.music.Autoplay_Engine.v3.constants import (
    AnalysisMode,
    AnalysisPriority,
    V3Config,
    PhysicsLayer,
    SemanticsLayer,
    LibrarianLayer,
    ANALYZER_CONFIG,
)
from modules.music.Autoplay_Engine.v3.mappings import SongIdentifier


class TestSongAnalyzerInitialization:
    """Tests for SongAnalyzer initialization."""
    
    def test_analyzer_creation(self):
        """Verify song analyzer can be created."""
        # Create with mocked dependencies
        with patch('modules.music.Autoplay_Engine.v3.song_analyzer.get_cache_manager') as mock_cache, \
             patch('modules.music.Autoplay_Engine.v3.song_analyzer.get_gemini_manager') as mock_gemini, \
             patch('modules.music.Autoplay_Engine.v3.song_analyzer.get_mappings_manager') as mock_mappings:
            
            mock_cache.return_value = MagicMock()
            mock_gemini.return_value = MagicMock()
            mock_mappings.return_value = MagicMock()
            
            analyzer = SongAnalyzer()
            assert analyzer is not None
            assert analyzer.config is not None
    
    def test_analyzer_with_config(self):
        """Verify song analyzer accepts V3Config."""
        with patch('modules.music.Autoplay_Engine.v3.song_analyzer.get_cache_manager') as mock_cache, \
             patch('modules.music.Autoplay_Engine.v3.song_analyzer.get_gemini_manager') as mock_gemini, \
             patch('modules.music.Autoplay_Engine.v3.song_analyzer.get_mappings_manager') as mock_mappings:
            
            mock_cache.return_value = MagicMock()
            mock_gemini.return_value = MagicMock()
            mock_mappings.return_value = MagicMock()
            
            config = V3Config()
            analyzer = SongAnalyzer(config=config)
            assert analyzer.config == config
    
    def test_analyzer_uses_analyzer_config(self):
        """Analyzer should use values from ANALYZER_CONFIG."""
        with patch('modules.music.Autoplay_Engine.v3.song_analyzer.get_cache_manager') as mock_cache, \
             patch('modules.music.Autoplay_Engine.v3.song_analyzer.get_gemini_manager') as mock_gemini, \
             patch('modules.music.Autoplay_Engine.v3.song_analyzer.get_mappings_manager') as mock_mappings:
            
            mock_cache.return_value = MagicMock()
            mock_gemini.return_value = MagicMock()
            mock_mappings.return_value = MagicMock()
            
            analyzer = SongAnalyzer()
            assert analyzer._worker_count == ANALYZER_CONFIG.analysis_worker_count


class TestAnalysisTask:
    """Tests for AnalysisTask dataclass."""
    
    def test_task_creation(self):
        """Should create analysis task with required fields."""
        identifier = SongIdentifier(
            deezer_id="123",
            title="Test Song",
            artist="Test Artist"
        )
        task = AnalysisTask(
            song_id="123",
            identifier=identifier,
            priority=AnalysisPriority.MEDIUM,
            mode=AnalysisMode.STANDARD,
            created_at=0.0
        )
        assert task.song_id == "123"
        assert task.priority == AnalysisPriority.MEDIUM
        assert task.mode == AnalysisMode.STANDARD
    
    def test_task_priority_ordering(self):
        """Tasks should order by priority (lower value = higher priority)."""
        identifier = SongIdentifier(deezer_id="1", title="Test", artist="Test")
        
        immediate = AnalysisTask(
            song_id="1",
            identifier=identifier,
            priority=AnalysisPriority.IMMEDIATE,
            mode=AnalysisMode.STANDARD,
            created_at=1.0
        )
        low = AnalysisTask(
            song_id="2",
            identifier=identifier,
            priority=AnalysisPriority.LOW,
            mode=AnalysisMode.STANDARD,
            created_at=0.0  # Earlier time but lower priority
        )
        
        # IMMEDIATE should come before LOW
        assert immediate < low
    
    def test_task_time_ordering_same_priority(self):
        """Tasks with same priority should order by creation time."""
        identifier = SongIdentifier(deezer_id="1", title="Test", artist="Test")
        
        earlier = AnalysisTask(
            song_id="1",
            identifier=identifier,
            priority=AnalysisPriority.HIGH,
            mode=AnalysisMode.STANDARD,
            created_at=100.0
        )
        later = AnalysisTask(
            song_id="2",
            identifier=identifier,
            priority=AnalysisPriority.HIGH,
            mode=AnalysisMode.STANDARD,
            created_at=200.0
        )
        
        assert earlier < later


class TestAnalysisPriorityEnum:
    """Tests for AnalysisPriority enum."""
    
    def test_all_priorities_exist(self):
        """Verify all priority levels exist."""
        assert hasattr(AnalysisPriority, 'IMMEDIATE')
        assert hasattr(AnalysisPriority, 'HIGH')
        assert hasattr(AnalysisPriority, 'MEDIUM')
        assert hasattr(AnalysisPriority, 'LOW')
        assert hasattr(AnalysisPriority, 'BATCH')
    
    def test_priority_ordering(self):
        """IMMEDIATE should have lowest value (highest priority)."""
        assert AnalysisPriority.IMMEDIATE.value < AnalysisPriority.HIGH.value
        assert AnalysisPriority.HIGH.value < AnalysisPriority.MEDIUM.value
        assert AnalysisPriority.MEDIUM.value < AnalysisPriority.LOW.value
        assert AnalysisPriority.LOW.value < AnalysisPriority.BATCH.value


class TestAnalysisLayers:
    """Tests for the three analysis layers."""
    
    def test_physics_layer_creation(self):
        """Should create PhysicsLayer with required fields."""
        physics = PhysicsLayer(
            computed_bpm=120.0,
            computed_key="C",
            computed_loudness=-10.0,
            timbre_vector=[0.0] * 13
        )
        assert physics.computed_bpm == 120.0
        assert physics.computed_key == "C"
    
    def test_semantics_layer_creation(self):
        """Should create SemanticsLayer with required fields."""
        semantics = SemanticsLayer(
            embedding_vector=[0.0] * 128,
            instrument_tags={"guitar": 0.9},
            quality_score=0.85
        )
        assert len(semantics.embedding_vector) == 128
        assert "guitar" in semantics.instrument_tags
    
    def test_librarian_layer_creation(self):
        """Should create LibrarianLayer with required fields."""
        librarian = LibrarianLayer(
            canonical_title="Bohemian Rhapsody",
            canonical_artist="Queen",
            release_era="1970s",
            cultural_vibe=["classic rock", "opera"],
            micro_genre=["progressive rock"],
            explicit_content=False
        )
        assert librarian.canonical_title == "Bohemian Rhapsody"
        assert librarian.explicit_content is False


class TestAnalysisModes:
    """Tests for AnalysisMode enum."""
    
    def test_all_modes_exist(self):
        """Verify all analysis modes exist."""
        assert hasattr(AnalysisMode, 'QUICK')
        assert hasattr(AnalysisMode, 'STANDARD')
        assert hasattr(AnalysisMode, 'DEEP')
        assert hasattr(AnalysisMode, 'FULL')
        assert hasattr(AnalysisMode, 'PHYSICS_ONLY')
    
    def test_mode_values_are_strings(self):
        """Mode values should be strings."""
        for mode in AnalysisMode:
            assert isinstance(mode.value, str)


class TestWorkerPoolConfiguration:
    """Tests for worker pool configuration."""
    
    def test_default_worker_count(self):
        """Default worker count should be 3."""
        assert ANALYZER_CONFIG.analysis_worker_count == 3
    
    def test_max_concurrent_downloads(self):
        """Should have concurrent download limit."""
        assert ANALYZER_CONFIG.max_concurrent_downloads >= 1
    
    def test_max_concurrent_analysis(self):
        """Should have concurrent analysis limit."""
        assert ANALYZER_CONFIG.max_concurrent_analysis >= 1


class TestGeminiBatchConfiguration:
    """Tests for Gemini batch processing configuration."""
    
    def test_batch_size_configuration(self):
        """Batch size should be configured."""
        assert ANALYZER_CONFIG.gemini_batch_size >= 1
    
    def test_batch_timeout_configuration(self):
        """Batch timeout should be configured."""
        assert ANALYZER_CONFIG.gemini_batch_timeout >= 0
    
    def test_batch_enabled_by_default(self):
        """Gemini batch processing should be enabled by default."""
        assert ANALYZER_CONFIG.gemini_batch_enabled is True


class TestSampleRateConfiguration:
    """Tests for audio sample rate configuration."""
    
    def test_sample_rate_is_22050(self):
        """Sample rate should be 22050 Hz for Librosa."""
        assert ANALYZER_CONFIG.sample_rate == 22050
    
    def test_hop_length_configured(self):
        """Hop length should be configured."""
        assert ANALYZER_CONFIG.hop_length >= 256


class TestAnalysisTimeout:
    """Tests for analysis timeout configuration."""
    
    def test_timeout_configured(self):
        """Analysis timeout should be configured."""
        assert ANALYZER_CONFIG.analysis_timeout_seconds >= 1
    
    def test_timeout_reasonable(self):
        """Timeout should be reasonable (not too long)."""
        assert ANALYZER_CONFIG.analysis_timeout_seconds <= 300  # Max 5 minutes
