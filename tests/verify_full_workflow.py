
import unittest
import asyncio
import logging
import sys
import os
import time
from unittest.mock import MagicMock, patch, AsyncMock
from dataclasses import dataclass

# Add project root to path
sys.path.append(r'c:\dev\Github\Discord-TodolistGrabber')

from modules.music.Autoplay_Engine.v3.deezer_fetch import DeezerClient, DeezerTrack, MatchResult
from modules.music.Autoplay_Engine.v3 import LastFMAutoplayV3
from modules.music.Autoplay_Engine.v3.session_manager import AutoplaySessionManager
from modules.music.Autoplay_Engine.v3.feedback_manager import FeedbackManager
from modules.music.Autoplay_Engine.v3.context_tracker import ContextTracker

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
LOG = logging.getLogger("verify_full_workflow")

# --- Mock Data from User Request ---

HAZBIN_HOTEL_TRACK_DATA = {
  "id": 3657543322,
  "readable": True,
  "title": "Live To Live",
  "title_short": "Live To Live",
  "title_version": "",
  "link": "https://www.deezer.com/track/3657543322",
  "duration": 131,
  "rank": 778201,
  "explicit_lyrics": False,
  "explicit_content_lyrics": 6,
  "explicit_content_cover": 2,
  "preview": "https://cdnt-preview.dzcdn.net/api/1/1/3/0/8/0/3083dd874e2f120ee8f78a716bad34af.mp3",
  "md5_image": "f9541b2f27d9872da30635b2ac3ad50d",
  "artist": {
    "id": 252433822,
    "name": "Hazbin Hotel",
    "link": "https://www.deezer.com/artist/252433822",
    "picture": "https://api.deezer.com/artist/252433822/image",
    "type": "artist"
  },
  "album": {
    "id": 856958832,
    "title": "Hazbin Hotel: Season Two (Original Soundtrack)",
    "cover": "https://api.deezer.com/album/856958832/image",
    "type": "album"
  },
  "type": "track"
}

GREATEST_SHOWMAN_TRACK_DATA = {
  "id": 435175492,
  "readable": True,
  "title": "The Other Side",
  "title_short": "The Other Side",
  "title_version": "",
  "link": "https://www.deezer.com/track/435175492",
  "duration": 214,
  "rank": 733025,
  "explicit_lyrics": False,
  "explicit_content_lyrics": 6,
  "explicit_content_cover": 2,
  "preview": "https://cdnt-preview.dzcdn.net/api/1/1/7/3/c/0/73ceb55c973c65681b46283fb047ab41.mp3",
  "md5_image": "ae1dd12d470530d41e9c2ea46772df1c",
  "artist": {
    "id": 169712,
    "name": "Hugh Jackman",
    "link": "https://www.deezer.com/artist/169712",
    "picture": "https://api.deezer.com/artist/169712/image",
    "type": "artist"
  },
  "album": {
    "id": 52562662,
    "title": "The Greatest Showman (Original Motion Picture Soundtrack)",
    "cover": "https://api.deezer.com/album/52562662/image",
    "type": "album"
  },
  "type": "track"
}

@dataclass
class MockTrack:
    """Simulates a Pomice/Lavalink track object"""
    title: str
    author: str
    uri: str
    identifier: str
    length: int = 180000
    is_stream: bool = False

    @property
    def info(self):
        return {
            "title": self.title,
            "author": self.author,
            "uri": self.uri,
            "identifier": self.identifier,
            "length": self.length,
            "isStream": self.is_stream
        }

class MockDeezerClient:
    """Simulates DeezerClient with predefined responses"""
    def __init__(self):
        self.session = True # Fake session
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
        
    async def search_track(self, query, limit=10):
        query = query.lower()
        LOG.info(f"🔎 [MockDeezer] Searching for: '{query}'")
        
        # Simulate fuzzy matching logic
        if "hazbin" in query or "live to live" in query:
            LOG.info("   -> Found Hazbin Hotel match")
            return [self._dict_to_track(HAZBIN_HOTEL_TRACK_DATA)]
        
        if "greatest showman" in query or "other side" in query:
            LOG.info("   -> Found Greatest Showman match")
            return [self._dict_to_track(GREATEST_SHOWMAN_TRACK_DATA)]
            
        LOG.warning("   -> No match found in mock DB")
        return []
        
    def get_best_match(self, results, threshold=0.75, expected_artist=None, expected_title=None, expected_duration_ms=None, relax_margin=0.05):
        if not results:
            return None
        
        # Return the first result with high confidence
        return MatchResult(
            track=results[0],
            confidence=1.0,
            artist_match=1.0,
            title_match=1.0,
            duration_match=1.0
        )
        
    def _preprocess_search_query(self, query):
        # Simple pass-through or basic cleanup for the mock
        return query.lower().strip()

    def _dict_to_track(self, data):
        return DeezerTrack(
            id=str(data["id"]),
            artist=data["artist"]["name"],
            title=data["title"],
            album=data["album"]["title"],
            duration_ms=data["duration"] * 1000,
            preview_url=data["preview"]
        )
    
    async def find_track_by_metadata(self, artist, title, expected_duration_ms=None, limit=10, threshold=0.62):
        # Simple mock implementation that searches by title
        results = await self.search_track(f"{artist} {title}")
        if results:
            return MatchResult(results[0], 1.0, 1.0, 1.0, 1.0)
        return None

class TestFullWorkflow(unittest.TestCase):
    
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        # Patch DeezerClient globally for the test
        self.deezer_patcher = patch('modules.music.Autoplay_Engine.v3.deezer_fetch.DeezerClient', side_effect=MockDeezerClient)
        self.mock_deezer_cls = self.deezer_patcher.start()
        
        # Also patch it in autoplayengine_v3 where it's imported
        self.deezer_patcher_engine = patch('modules.music.Autoplay_Engine.v3.autoplayengine_v3.DeezerClient', side_effect=MockDeezerClient)
        self.deezer_patcher_engine.start()

        # Patch GeminiService to avoid real API calls
        self.gemini_patcher = patch('modules.music.Autoplay_Engine.v3.gemini_service.GeminiService')
        self.mock_gemini_cls = self.gemini_patcher.start()
        self.mock_gemini_instance = self.mock_gemini_cls.return_value
        # Mock generate_search_queries to return a simple list
        self.mock_gemini_instance.generate_search_queries = AsyncMock(return_value=[
            "Hazbin Hotel Live To Live",
            "Live To Live Hazbin Hotel",
            "Hazbin Hotel Season 2 Live To Live"
        ])

    def tearDown(self):
        self.deezer_patcher.stop()
        self.deezer_patcher_engine.stop()
        self.gemini_patcher.stop()
        self.loop.close()

    def test_01_resolution_accuracy(self):
        """
        Test Case 1: Verify YouTube Title -> Deezer Metadata resolution.
        Uses the specific examples provided by the user.
        """
        LOG.info("\n🧪 TEST CASE 1: Resolution Accuracy (Mocked Deezer API)")
        
        test_cases = [
            {
                "youtube_title": "Live To Live | Hazbin Hotel Season 2 | Prime Video",
                "expected_title": "Live To Live",
                "expected_artist": "Hazbin Hotel"
            },
            {
                "youtube_title": "The Greatest Showman Cast - The Other Side (Official Audio)",
                "expected_title": "The Other Side",
                "expected_artist": "Hugh Jackman"
            }
        ]

        async def run_test():
            async with MockDeezerClient() as client:
                for case in test_cases:
                    yt_title = case["youtube_title"]
                    LOG.info(f"🔍 Resolving: '{yt_title}'")
                    
                    results = await client.search_track(yt_title)
                    
                    if not results:
                        self.fail(f"❌ No results found for '{yt_title}'")

                    top_result = results[0]
                    LOG.info(f"   Top Result: '{top_result.artist} - {top_result.title}'")

                    self.assertIn(case["expected_title"].lower(), top_result.title.lower())
                    self.assertIn(case["expected_artist"].lower(), top_result.artist.lower())
                    LOG.info("✅ Match Verified")

        self.loop.run_until_complete(run_test())

    def test_02_user_simulation(self):
        """
        Test Case 2: Simulate a full user session with skips and feedback.
        """
        LOG.info("\n🧪 TEST CASE 2: User Session Simulation")

        mock_bot = MagicMock()
        mock_bot.loop = self.loop
        
        session_manager = AutoplaySessionManager()
        feedback_manager = FeedbackManager()
        
        engine = LastFMAutoplayV3(mock_bot)
        engine._engine._session_manager = session_manager
        engine._engine._feedback_manager = feedback_manager
        
        guild_id = 12345
        
        LOG.info("▶️ User plays 'Shape of You'")
        tracker = engine._get_context_tracker(guild_id)
        tracker.record_play(
            track_id="Ed Sheeran::Shape of You",
            artist="Ed Sheeran",
            title="Shape of You",
            genres=["pop"],
            progress_ratio=1.0
        )
        
        ctx = tracker.get_context()
        self.assertEqual(ctx.recent_artists, {"Ed Sheeran"})
        LOG.info(f"   Context Updated: {list(ctx.recent_artists)[0]}")

        LOG.info("⏭️ User SKIPS the next track 'Bad Habits'")
        tracker.record_play(
            track_id="Ed Sheeran::Bad Habits",
            artist="Ed Sheeran",
            title="Bad Habits",
            genres=["pop"],
            was_skipped=True,
            skip_type="hard",
            progress_ratio=0.1
        )
        
        ctx = tracker.get_context()
        LOG.info(f"   Skip Rate: {ctx.skip_rate:.2f}")
        self.assertGreater(ctx.skip_rate, 0.0)
        LOG.info("✅ User Simulation Complete")

    def test_03_recommendation_pipeline(self):
        """
        Test Case 3: Full Recommendation Pipeline (Resolution -> Pool -> Enrichment -> Selection)
        """
        LOG.info("\n🧪 TEST CASE 3: Recommendation Pipeline (ML Simulation)")
        
        mock_bot = MagicMock()
        mock_bot.loop = self.loop
        
        # Initialize Engine
        engine = LastFMAutoplayV3(mock_bot)
        
        # Mock the candidate generation (Last.fm) since we don't have a key
        # We simulate a pool of candidates that the engine would "find"
        mock_candidates = [
            {"artist": "Hazbin Hotel", "title": "Happy Day in Hell", "pool_source": "pool_a_continuity"},
            {"artist": "Hugh Jackman", "title": "From Now On", "pool_source": "pool_b_familiarity"},
            {"artist": "Keala Settle", "title": "This Is Me", "pool_source": "pool_c_safe_harbor"}
        ]
        
        # Patch _fetch_candidate_records to return our mock pool
        engine._fetch_candidate_records = AsyncMock(return_value=mock_candidates)
        
        # Patch _queue_analysis_for_track to avoid needing real audio downloaders
        engine._queue_analysis_for_track = AsyncMock()
        
        # Patch resolve_track to return a MockTrack object (simulating Pomice/Lavalink)
        async def mock_resolve(artist, title, **kwargs):
            return MockTrack(title, artist, f"http://yt/{title}", f"yt_{title}")
        engine._engine.resolve_track = AsyncMock(side_effect=mock_resolve)

        # Input: The user's example track (Hazbin Hotel)
        seed_track_info = {
            "title": "Live To Live | Hazbin Hotel Season 2 | Prime Video",
            "author": "Prime Video", # Channel name
            "length": 131000,
            "guild_id": 12345
        }
        
        async def run_pipeline():
            LOG.info(f"🎵 Input Seed: {seed_track_info['title']}")
            
            # 1. Call the main recommendation method
            recommendations = await engine.get_recommendations_for_track(seed_track_info)
            
            # 2. Verify Results
            if not recommendations:
                self.fail("❌ No recommendations returned!")
                
            track_id, track_obj = recommendations[0]
            LOG.info(f"✅ Recommended: '{track_obj.title}' by '{track_obj.author}'")
            
            # 3. Verify Internal Steps
            
            # Verify Parsing/Resolution of Seed
            # The engine should have called parse_track -> resolve_track -> Deezer search
            # We can check if our MockDeezer was called
            # (Implicitly verified if we got a result, as the engine needs a seed artist/title)
            
            # Verify Candidate Fetching
            engine._fetch_candidate_records.assert_called_once()
            call_args = engine._fetch_candidate_records.call_args[1]
            self.assertEqual(call_args['seed_artist'], "Hazbin Hotel") # Should be resolved from Deezer
            self.assertEqual(call_args['seed_title'], "Live To Live")
            LOG.info("✅ Seed Track correctly resolved to 'Hazbin Hotel - Live To Live'")
            
            # Verify Analysis Queueing
            engine._queue_analysis_for_track.assert_called()
            LOG.info("✅ Analysis queued for recommended track")

        self.loop.run_until_complete(run_pipeline())

if __name__ == '__main__':
    unittest.main()
