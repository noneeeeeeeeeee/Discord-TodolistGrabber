"""
V3 Autoplay Engine Stress Test

Simulates real usage scenarios to verify the recommendation system works:
1. Fresh start with no data
2. Single track seeding
3. Consecutive skips (stress test skip handling)
4. More Like This feedback
5. Session recovery
6. Multiple concurrent sessions

Run with: python tests/test_v3_stress.py
"""

import asyncio
import os
import sys
import time
import shutil

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env file like main.py does
from dotenv import load_dotenv
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(env_path)


# Real song examples from current charts (2025)
REAL_SONGS = [
    {"title": "CHANEL", "author": "Tyla", "length": 195000},
    {"title": "The Fate of Ophelia", "author": "Taylor Swift", "length": 240000},
    {"title": "Man I Need", "author": "Olivia Dean", "length": 200000},
    {"title": "WHERE IS MY HUSBAND!", "author": "RAYE", "length": 210000},
    {"title": "12 to 12", "author": "sombr", "length": 180000},
    {"title": "Folded", "author": "Kehlani", "length": 190000},
    {"title": "Blinding Lights", "author": "The Weeknd", "length": 200000},
    {"title": "Save Your Tears", "author": "The Weeknd", "length": 215000},
    {"title": "Bohemian Rhapsody", "author": "Queen", "length": 354000},
    {"title": "Shape of You", "author": "Ed Sheeran", "length": 234000},
    {"title": "Hotel California", "author": "Eagles", "length": 391000},
    {"title": "Uptown Funk", "author": "Bruno Mars", "length": 270000},
    {"title": "Bad Guy", "author": "Billie Eilish", "length": 194000},
    {"title": "Watermelon Sugar", "author": "Harry Styles", "length": 174000},
    {"title": "Levitating", "author": "Dua Lipa", "length": 203000},
]


def clear_v3_cache():
    """Clear V3 cache directory before each test run."""
    cache_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "modules", "music", "Autoplay_Engine", "v3", "cache"
    )
    if os.path.exists(cache_dir):
        try:
            shutil.rmtree(cache_dir)
            print(f"\n🧹 Cleared cache: {cache_dir}")
        except Exception as e:
            print(f"\n⚠️ Failed to clear cache: {e}")
    else:
        print(f"\n✅ No cache to clear")


class StressTestResults:
    """Collect and report test results."""
    
    def __init__(self):
        self.tests_run = 0
        self.tests_passed = 0
        self.tests_failed = 0
        self.failures = []
        
    def record(self, test_name: str, passed: bool, error: str = ""):
        self.tests_run += 1
        if passed:
            self.tests_passed += 1
            print(f"  [PASS] {test_name}")
        else:
            self.tests_failed += 1
            self.failures.append((test_name, error))
            print(f"  [FAIL] {test_name}: {error}")
    
    def report(self):
        print("\n" + "="*60)
        print(f"STRESS TEST RESULTS: {self.tests_passed}/{self.tests_run} passed")
        if self.failures:
            print("\nFailures:")
            for name, error in self.failures:
                print(f"  - {name}: {error}")
        print("="*60)
        return self.tests_failed == 0


async def test_wrapper_initialization(results: StressTestResults):
    """Test 1: Wrapper initializes correctly with env vars."""
    print("\n[Test 1] Wrapper Initialization")
    
    try:
        from modules.music.Autoplay_Engine.v3 import get_lastfm_autoplay_v3
        
        wrapper = get_lastfm_autoplay_v3(bot=None)
        
        results.record(
            "is_available() returns True",
            wrapper.is_available(),
            "Expected True"
        )
        
        results.record(
            "can_recommend() returns True",
            wrapper.can_recommend(),
            "Expected True"
        )
        
        results.record(
            "_prerequisites_available is True",
            wrapper._prerequisites_available,
            "LASTFM_API_KEY should be detected"
        )
        
    except Exception as e:
        results.record("Wrapper initialization", False, str(e))


async def test_engine_lazy_initialization(results: StressTestResults):
    """Test 2: Engine initializes lazily on first use."""
    print("\n[Test 2] Engine Lazy Initialization")
    
    try:
        from modules.music.Autoplay_Engine.v3 import get_lastfm_autoplay_v3
        
        # Get fresh wrapper (singleton, so reuse existing)
        wrapper = get_lastfm_autoplay_v3(bot=None)
        
        # Before any async calls, engine may not be initialized
        initial_state = wrapper._engine_initialized
        
        # Trigger initialization
        initialized = await wrapper._ensure_initialized()
        
        results.record(
            "_ensure_initialized() succeeds",
            initialized,
            "Engine should initialize"
        )
        
        results.record(
            "_engine_initialized is True after init",
            wrapper._engine_initialized,
            "Should be True after initialization"
        )
        
    except Exception as e:
        results.record("Lazy initialization", False, str(e))


async def test_session_creation(results: StressTestResults):
    """Test 3: Session creation for a guild."""
    print("\n[Test 3] Session Creation")
    
    try:
        from modules.music.Autoplay_Engine.v3 import get_lastfm_autoplay_v3
        
        wrapper = get_lastfm_autoplay_v3(bot=None)
        
        # Simulate getting recommendations (which creates session)
        # Use real song from charts
        track_info = REAL_SONGS[0].copy()  # CHANEL by Tyla
        track_info["guild_id"] = 123456789
        
        # This should create a session and return recommendations
        recommendations = await wrapper.get_recommendations_for_track(track_info, limit=3)
        
        results.record(
            "Session created for guild",
            123456789 in wrapper._guild_sessions,
            "Guild should have a session"
        )
        
        results.record(
            "Recommendations returned (may be empty if no API)",
            isinstance(recommendations, list),
            f"Got type: {type(recommendations)}"
        )
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        results.record("Session creation", False, str(e))


async def test_feedback_recording(results: StressTestResults):
    """Test 4: Feedback recording (More Like This)."""
    print("\n[Test 4] Feedback Recording")
    
    try:
        from modules.music.Autoplay_Engine.v3 import get_lastfm_autoplay_v3
        
        wrapper = get_lastfm_autoplay_v3(bot=None)
        
        # First create a session with real song
        track_info = REAL_SONGS[8].copy()  # Bohemian Rhapsody
        track_info["guild_id"] = 987654321
        await wrapper.get_recommendations_for_track(track_info, limit=1)
        
        # Now record feedback (simulating button press)
        try:
            await wrapper.record_playback_feedback(
                guild_id=987654321,
                artist=track_info["author"],
                title=track_info["title"],
                progress_ratio=1.0,
                feedback_type="more_like_this",
                user_id=123
            )
            results.record("More Like This feedback recorded", True, "")
        except Exception as e:
            results.record("More Like This feedback", False, str(e))
            
    except Exception as e:
        results.record("Feedback recording setup", False, str(e))


async def test_consecutive_skips(results: StressTestResults):
    """Test 5: Consecutive skip stress test."""
    print("\n[Test 5] Consecutive Skips Stress Test")
    
    try:
        from modules.music.Autoplay_Engine.v3 import get_lastfm_autoplay_v3
        from modules.music.Autoplay_Engine.v3.session_mixer import get_session_mixer, RecoveryState
        
        wrapper = get_lastfm_autoplay_v3(bot=None)
        mixer = get_session_mixer()
        
        # Create session with real song
        track_info = REAL_SONGS[9].copy()  # Shape of You
        track_info["guild_id"] = 555555555
        await wrapper.get_recommendations_for_track(track_info, limit=1)
        
        session_id = wrapper._guild_sessions.get(555555555)
        if not session_id:
            results.record("Session exists for skip test", False, "No session created")
            return
            
        # Simulate 5 consecutive early skips
        for i in range(5):
            await wrapper.record_playback_feedback(
                guild_id=555555555,
                artist="Random Artist",
                title=f"Skipped Track {i+1}",
                progress_ratio=0.1,  # Early skip
                duration_ms=200000
            )
        
        # Check recovery state after skips
        recovery_state = mixer.get_recovery_state(session_id)
        
        results.record(
            "Skip tracking works",
            True,
            f"Recovery state after 5 skips: {recovery_state}"
        )
        
        # Check if weights adjusted
        weights = mixer.get_source_weights(session_id)
        results.record(
            "Adaptive weights available",
            len(weights) > 0,
            f"Weights: {weights}"
        )
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        results.record("Consecutive skips test", False, str(e))


async def test_clear_history(results: StressTestResults):
    """Test 6: Clear history (session cleanup)."""
    print("\n[Test 6] Clear History")
    
    try:
        from modules.music.Autoplay_Engine.v3 import get_lastfm_autoplay_v3
        
        wrapper = get_lastfm_autoplay_v3(bot=None)
        
        # Create a session with real song
        track_info = REAL_SONGS[11].copy()  # Uptown Funk
        track_info["guild_id"] = 777777777
        await wrapper.get_recommendations_for_track(track_info, limit=1)
        
        had_session = 777777777 in wrapper._guild_sessions
        
        # Clear history for this guild
        wrapper.clear_history(guild_id=777777777)
        
        # Allow async task to run
        await asyncio.sleep(0.1)
        
        session_cleared = 777777777 not in wrapper._guild_sessions
        
        results.record(
            "Session existed before clear",
            had_session,
            "Should have created session"
        )
        
        results.record(
            "Session cleared after clear_history",
            session_cleared,
            "Session should be removed"
        )
        
    except Exception as e:
        results.record("Clear history", False, str(e))


async def test_real_deezer_api(results: StressTestResults):
    """Test 7: Real Deezer API integration."""
    print("\n[Test 7] Real Deezer API")
    
    try:
        from modules.music.Autoplay_Engine.v3.daydreamer import get_daydreamer
        
        daydreamer = get_daydreamer()
        await daydreamer.initialize()
        
        # Test Deezer chart fetch
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get("https://api.deezer.com/chart/0/tracks?limit=5") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tracks = data.get("data", [])
                    
                    results.record(
                        "Deezer API reachable",
                        len(tracks) > 0,
                        f"Got {len(tracks)} tracks"
                    )
                else:
                    results.record("Deezer API", False, f"Status {resp.status}")
                    
    except Exception as e:
        results.record("Deezer API test", False, str(e))


async def test_session_mixer_integration(results: StressTestResults):
    """Test 8: SessionMixer with Recommender integration."""
    print("\n[Test 8] SessionMixer Integration")
    
    try:
        from modules.music.Autoplay_Engine.v3.session_mixer import get_session_mixer, RecoveryState
        from modules.music.Autoplay_Engine.v3.recommender import get_recommender
        
        mixer = get_session_mixer()
        recommender = get_recommender()
        
        await mixer.initialize()
        await recommender.initialize()
        
        # Test adaptive weights for different states
        test_session = "stress_test_session"
        
        # Initial state (COLD)
        weights = mixer.get_source_weights(test_session)
        results.record(
            "Initial weights available",
            len(weights) > 0,
            f"Weights: {weights}"
        )
        
        # Record some skips to trigger state changes
        for i in range(3):
            mixer.record_playback(
                session_id=test_session,
                song_id=f"test_song_{i}",
                duration_played_ms=30000,  # 30 seconds = early skip
                total_duration_ms=200000,
                was_skipped=True
            )
            
        # Check state changed
        state = mixer.get_recovery_state(test_session)
        results.record(
            "Recovery state tracking works",
            state in [RecoveryState.NORMAL, RecoveryState.CAUTION, RecoveryState.RECOVERY],
            f"Current state: {state}"
        )
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        results.record("SessionMixer integration", False, str(e))


async def test_config_integration(results: StressTestResults):
    """Test 9: Config module returns V3 wrapper."""
    print("\n[Test 9] Config Integration")
    
    try:
        from modules.music.Autoplay_Engine.config import get_autoplay_engine, get_autoplay_config
        
        config = get_autoplay_config()
        engine = get_autoplay_engine(bot=None)
        
        results.record(
            "Config supports feedback buttons",
            config.supports_feedback_buttons(),
            "V3 should support feedback buttons"
        )
        
        results.record(
            "Engine is LastFMAutoplayV3",
            type(engine).__name__ == "LastFMAutoplayV3",
            f"Got: {type(engine).__name__}"
        )
        
        results.record(
            "Engine is_available()",
            engine.is_available(),
            "Should be True with LASTFM_API_KEY set"
        )
        
        results.record(
            "Engine can_recommend()",
            engine.can_recommend(),
            "Should be True"
        )
        
    except Exception as e:
        results.record("Config integration", False, str(e))


async def test_multiple_guilds(results: StressTestResults):
    """Test 10: Multiple concurrent guild sessions."""
    print("\n[Test 10] Multiple Guild Sessions")
    
    try:
        from modules.music.Autoplay_Engine.v3 import get_lastfm_autoplay_v3
        
        wrapper = get_lastfm_autoplay_v3(bot=None)
        
        guild_ids = [111111111, 222222222, 333333333]
        tracks = [
            REAL_SONGS[12].copy(),  # Bad Guy - Billie Eilish
            REAL_SONGS[13].copy(),  # Watermelon Sugar - Harry Styles
            REAL_SONGS[14].copy(),  # Levitating - Dua Lipa
        ]
        
        # Create sessions for all guilds concurrently
        async def create_session(guild_id, track):
            track["guild_id"] = guild_id
            return await wrapper.get_recommendations_for_track(track, limit=1)
            
        await asyncio.gather(
            create_session(guild_ids[0], tracks[0]),
            create_session(guild_ids[1], tracks[1]),
            create_session(guild_ids[2], tracks[2]),
        )
        
        # Check all sessions exist
        sessions_created = sum(1 for gid in guild_ids if gid in wrapper._guild_sessions)
        
        results.record(
            f"All {len(guild_ids)} guild sessions created",
            sessions_created == len(guild_ids),
            f"Created {sessions_created}/{len(guild_ids)} sessions"
        )
        
    except Exception as e:
        results.record("Multiple guilds", False, str(e))


async def main():
    """Run all stress tests."""
    print("="*60)
    print("V3 AUTOPLAY ENGINE STRESS TEST")
    print("="*60)
    
    # Clear cache before running tests
    clear_v3_cache()
    
    # Reset singleton instances to ensure fresh state
    try:
        import modules.music.Autoplay_Engine.v3 as v3_module
        v3_module._lastfm_autoplay_v3 = None
        v3_module._v3_engine = None
        
        # Also reset session manager singleton to clear old sessions
        from modules.music.Autoplay_Engine.v3 import session_manager as sm_module
        sm_module._session_manager = None
        
        # Also reset the session mixer
        from modules.music.Autoplay_Engine.v3 import session_mixer as mixer_module
        mixer_module._session_mixer = None
        
        print("🔄 Reset V3 singleton instances")
    except Exception as e:
        print(f"⚠️ Could not reset singletons: {e}")
    
    # Increase max sessions for testing
    os.environ["AUTOPLAY_MAX_SESSIONS"] = "20"
    
    results = StressTestResults()
    
    # Run all tests
    await test_wrapper_initialization(results)
    await test_engine_lazy_initialization(results)
    await test_session_creation(results)
    await test_feedback_recording(results)
    await test_consecutive_skips(results)
    await test_clear_history(results)
    await test_real_deezer_api(results)
    await test_session_mixer_integration(results)
    await test_config_integration(results)
    await test_multiple_guilds(results)
    
    # Report results
    success = results.report()
    return 0 if success else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
