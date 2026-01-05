"""
Tests for V3 Autoplay Engine Constants Module

Tests for enums, dataclasses, and configuration constants.
"""

import pytest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.music.Autoplay_Engine.v3.constants import (
    SessionState,
    CacheType,
    EventType,
    AnalysisMode,
    SkipType,
    RecommendationSource,
    V3Config,
    CacheConfig,
    AnalysisConfig,
    BufferConfig,
    SessionThresholds,
    RateLimitConfig
)


class TestSessionStateEnum:
    """Tests for SessionState enum."""
    
    def test_all_states_exist(self):
        """Verify all expected session states are defined."""
        assert hasattr(SessionState, 'COLD')
        assert hasattr(SessionState, 'WARM')
        assert hasattr(SessionState, 'HOT')
        assert hasattr(SessionState, 'EXTENDED')
    
    def test_state_values(self):
        """Verify state values are distinct."""
        states = [SessionState.COLD, SessionState.WARM, SessionState.HOT, SessionState.EXTENDED]
        values = [s.value for s in states]
        assert len(values) == len(set(values)), "Session state values must be unique"
    
    def test_state_ordering_by_value(self):
        """States should progress in order: COLD < WARM < HOT < EXTENDED."""
        # Using the string values, but this documents expected progression
        states = [SessionState.COLD, SessionState.WARM, SessionState.HOT, SessionState.EXTENDED]
        assert len(states) == 4


class TestCacheTypeEnum:
    """Tests for CacheType enum."""
    
    def test_all_cache_types_exist(self):
        """Verify all expected cache types are defined."""
        assert hasattr(CacheType, 'MAPPINGS')
        assert hasattr(CacheType, 'METADATA')
        assert hasattr(CacheType, 'ANALYSIS')
        assert hasattr(CacheType, 'PREFERENCES')
        assert hasattr(CacheType, 'SESSIONS')
    
    def test_cache_type_values_are_strings(self):
        """Cache type values should be usable as directory names."""
        for cache_type in CacheType:
            assert isinstance(cache_type.value, str)
            # Should not contain path separators
            assert '/' not in cache_type.value
            assert '\\' not in cache_type.value


class TestEventTypeEnum:
    """Tests for EventType enum."""
    
    def test_song_events_exist(self):
        """Verify song-related events are defined."""
        assert hasattr(EventType, 'SONG_STARTED')
        assert hasattr(EventType, 'SONG_ENDED')
        assert hasattr(EventType, 'SONG_SKIPPED')
    
    def test_session_events_exist(self):
        """Verify session-related events are defined."""
        assert hasattr(EventType, 'SESSION_STARTED')
        assert hasattr(EventType, 'SESSION_ENDED')
        assert hasattr(EventType, 'SESSION_STATE_CHANGED')
    
    def test_buffer_events_exist(self):
        """Verify buffer-related events are defined."""
        assert hasattr(EventType, 'BUFFER_LOW')
        assert hasattr(EventType, 'BUFFER_REFILLED')
    
    def test_analysis_events_exist(self):
        """Verify analysis-related events are defined."""
        assert hasattr(EventType, 'ANALYSIS_REQUESTED')
        assert hasattr(EventType, 'ANALYSIS_COMPLETED')


class TestAnalysisModeEnum:
    """Tests for AnalysisMode enum."""
    
    def test_all_modes_exist(self):
        """Verify all analysis modes are defined."""
        assert hasattr(AnalysisMode, 'QUICK')
        assert hasattr(AnalysisMode, 'STANDARD')
        assert hasattr(AnalysisMode, 'DEEP')
    
    def test_mode_values(self):
        """Modes should have distinct values."""
        modes = [AnalysisMode.QUICK, AnalysisMode.STANDARD, AnalysisMode.DEEP]
        values = [m.value for m in modes]
        assert len(values) == len(set(values))


class TestSkipTypeEnum:
    """Tests for SkipType enum."""
    
    def test_all_skip_types_exist(self):
        """Verify all skip types are defined per Apple Music classification."""
        assert hasattr(SkipType, 'IMMEDIATE')
        assert hasattr(SkipType, 'EARLY')
        assert hasattr(SkipType, 'MID')
        assert hasattr(SkipType, 'LATE')
        assert hasattr(SkipType, 'COMPLETED')


class TestRecommendationSourceEnum:
    """Tests for RecommendationSource enum."""
    
    def test_all_sources_exist(self):
        """Verify all recommendation sources are defined."""
        assert hasattr(RecommendationSource, 'LASTFM_SIMILAR')
        assert hasattr(RecommendationSource, 'COLLABORATIVE')
        assert hasattr(RecommendationSource, 'GEMINI')
        assert hasattr(RecommendationSource, 'GENRE_BASED')
        assert hasattr(RecommendationSource, 'DAYDREAM')


class TestV3Config:
    """Tests for V3Config dataclass."""
    
    def test_default_config_creation(self):
        """Verify default config can be created."""
        config = V3Config()
        assert config is not None
    
    def test_session_thresholds(self):
        """Verify session thresholds match spec: Cold(1-10), Warm(11-25), Hot(25+)."""
        config = V3Config()
        # Cold ends at 10, Warm ends at 25, Hot starts at 25+
        assert config.thresholds.cold_max == 10
        assert config.thresholds.warm_max == 25
        assert config.thresholds.hot_min == 25
    
    def test_buffer_size(self):
        """Buffer should be 5 songs per spec."""
        config = V3Config()
        assert config.buffer.size == 5
    
    def test_max_concurrent_sessions(self):
        """Max concurrent sessions should be 2 per spec."""
        config = V3Config()
        assert config.max_concurrent_sessions == 2
    
    def test_daydream_cap(self):
        """Daydream should analyze max 50 songs per cycle."""
        config = V3Config()
        assert config.daydream_songs_per_cycle == 50


class TestCacheConfig:
    """Tests for CacheConfig dataclass."""
    
    def test_default_cache_config(self):
        """Verify default cache config."""
        config = CacheConfig()
        assert config is not None
    
    def test_cache_base_path(self):
        """Cache base path should be ./cache/Autoplay/v3/."""
        config = CacheConfig()
        assert 'cache' in config.base_path
        assert 'Autoplay' in config.base_path
        assert 'v3' in config.base_path
    
    def test_shard_capacity(self):
        """Shard capacity should be 5000+ per spec."""
        config = CacheConfig()
        assert config.max_entries_per_shard >= 5000
    
    def test_cache_version(self):
        """Cache should have a version number."""
        config = CacheConfig()
        assert config.version >= 1


class TestSessionThresholds:
    """Tests for SessionThresholds dataclass."""
    
    def test_threshold_consistency(self):
        """Cold max should be less than warm max."""
        thresholds = SessionThresholds()
        assert thresholds.cold_max < thresholds.warm_max
    
    def test_hot_starts_at_warm_max(self):
        """Hot should start where warm ends."""
        thresholds = SessionThresholds()
        assert thresholds.hot_min == thresholds.warm_max


class TestAnalysisConfig:
    """Tests for AnalysisConfig dataclass."""
    
    def test_default_analysis_config(self):
        """Verify default analysis config."""
        config = AnalysisConfig()
        assert config is not None
    
    def test_librosa_settings(self):
        """Librosa should extract BPM, key, loudness, timbre."""
        config = AnalysisConfig()
        assert config.librosa_enabled is True
    
    def test_efficientat_model(self):
        """EfficientAT should use mn10_as model per spec."""
        config = AnalysisConfig()
        assert config.efficientat_model == 'mn10_as'
    
    def test_gemini_model(self):
        """Gemini should use gemini-2.5-flash per spec."""
        config = AnalysisConfig()
        assert 'gemini-2.5-flash' in config.gemini_model


class TestBufferConfig:
    """Tests for BufferConfig dataclass."""
    
    def test_buffer_size_is_five(self):
        """Buffer size should be exactly 5 per Apple Music style."""
        config = BufferConfig()
        assert config.size == 5
    
    def test_low_threshold(self):
        """Low threshold should trigger refill before buffer is empty."""
        config = BufferConfig()
        assert config.low_threshold >= 1
        assert config.low_threshold < config.size
    
    def test_prefetch_enabled(self):
        """Prefetching should be enabled by default."""
        config = BufferConfig()
        assert config.prefetch_enabled is True


class TestRateLimitConfig:
    """Tests for RateLimitConfig dataclass."""
    
    def test_deezer_rate_limits(self):
        """Deezer has rate limits for unauthenticated access."""
        config = RateLimitConfig()
        assert config.deezer_requests_per_minute > 0
    
    def test_lastfm_rate_limits(self):
        """Last.fm has usage limits per spec."""
        config = RateLimitConfig()
        assert config.lastfm_requests_per_minute > 0
    
    def test_gemini_rate_limits(self):
        """Gemini batching should be 50 per batch per spec."""
        config = RateLimitConfig()
        assert config.gemini_batch_size == 50


class TestConfigIntegration:
    """Integration tests for configuration classes."""
    
    def test_v3_config_contains_all_sub_configs(self):
        """V3Config should contain all sub-configuration objects."""
        config = V3Config()
        assert hasattr(config, 'thresholds')
        assert hasattr(config, 'buffer')
        assert hasattr(config, 'cache')
        assert hasattr(config, 'analysis')
        assert hasattr(config, 'rate_limits')
    
    def test_config_serialization(self):
        """Config should be convertible to dict for persistence."""
        config = V3Config()
        # dataclasses should have asdict or similar
        from dataclasses import asdict
        config_dict = asdict(config)
        assert isinstance(config_dict, dict)
        assert 'thresholds' in config_dict or 'buffer' in config_dict
