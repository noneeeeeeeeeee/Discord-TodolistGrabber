import asyncio
import logging
import os
import sys
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, os.getcwd())

# Patch DependencyManager to avoid downloads
patcher = patch("modules.music.Autoplay_Engine.v3.autoplayengine_v3.DependencyManager")
MockDependencyManager = patcher.start()
MockDependencyManager.return_value.ensure_ready.return_value = None
MockDependencyManager.return_value.ensure_ffmpeg.return_value = None
MockDependencyManager.return_value.ensure_model.return_value = None

from modules.music.Autoplay_Engine.v3 import LastFMAutoplayV3, PreparedCandidate
from modules.music.Autoplay_Engine.v3.deezer_fetch import DeezerClient
from modules.music.Autoplay_Engine.v3.contextual_recommender import CandidateFeatures, ScoredCandidate

# Configure logging
logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger("verify_progressive")

async def main():
    LOG.info("🚀 Starting Progressive Logic Verification")
    
    # Initialize Orchestrator
    bot = MagicMock()
    autoplay = LastFMAutoplayV3(bot)
    
    # 1. Populate Bootstrap Manager with REAL Deezer tracks
    LOG.info("🎵 Fetching REAL charts from Deezer for Bootstrap Pool...")
    try:
        async with DeezerClient() as client:
            charts = await client.get_charts(limit=10)
        LOG.info(f"✅ Fetched {len(charts)} tracks from Deezer.")
    except Exception as e:
        LOG.warning(f"⚠️ Failed to fetch from Deezer: {e}")
        charts = []
        # Fallback for offline testing
        class DummyTrack:
            artist = "Test Artist"
            title = "Test Title"
        charts = [DummyTrack()]

    # Inject into BootstrapManager
    for track in charts:
        key = f"{track.artist}::{track.title}"
        autoplay._engine._bootstrap_manager._bootstrapped_tracks.add(key)
        
    LOG.info(f"🌱 Bootstrap Manager has {len(autoplay._engine._bootstrap_manager._bootstrapped_tracks)} tracks.")

    # Mock _prepare_candidates to return dummy candidates
    async def mock_prepare(guild_id, candidates):
        prepared = []
        for c in candidates:
            # Create dummy features
            features = CandidateFeatures(
                track_id=f"{c['artist']}::{c['title']}",
                artist=c['artist'],
                title=c['title'],
                content_similarity=0.5,
                session_similarity=0.5,
                novelty=0.5,
                quality=0.5,
                genres=[],
                mood_vector=[],
                mood_label="happy",
                energy=0.5,
                computed_tempo=120.0,
                computed_key=0,
                computed_mode=1,
                computed_loudness=-5.0
            )
            prepared.append(PreparedCandidate(features=features, metadata=c))
        return prepared
    autoplay._prepare_candidates = mock_prepare
    
    # Mock score_candidates to return the candidates as is (with dummy scores)
    def mock_score(guild_id, features, **kwargs):
        scored = []
        for f in features:
            scored.append(ScoredCandidate(
                track_id=f.track_id,
                title=f.track_id.split("::")[1],
                artist=f.track_id.split("::")[0],
                score=0.9, # High score to ensure selection
                breakdown={"base": 0.9, "final": 0.9}
            ))
        return scored
    autoplay._engine.score_candidates = mock_score

    # --- TEST CASE 1: Cold Start (< 50 cached) ---
    LOG.info("\n🧪 TEST CASE 1: System Cold Start (< 50 cached tracks)")
    
    # Mock cache size
    autoplay._engine._cache._enrichment_cache = {str(i): i for i in range(10)}
    LOG.info(f"📊 Mocked Cache Size: {len(autoplay._engine._cache._enrichment_cache)}")
    
    # Mock session manager
    autoplay._engine._session_manager.acquire = MagicMock(return_value=asyncio.Future())
    autoplay._engine._session_manager.acquire.return_value.set_result(True)
    
    # Mock resolve_track to always succeed
    async def mock_resolve(artist, title, **kwargs):
        return MagicMock(info={"identifier": "mock_id", "title": title, "author": artist})
    autoplay._engine.resolve_track = mock_resolve
    
    # Mock parse_track
    async def mock_parse(title, artist):
        return {"artist": "Ed Sheeran", "title": "Shape of You", "is_canonical": True}
    autoplay._engine.parse_track = mock_parse

    # Seed track
    seed_track = {"title": "Shape of You", "author": "Ed Sheeran", "guild_id": 123}
    
    # Invoke
    recommendations = await autoplay.get_recommendations_for_track(seed_track)
    
    LOG.info(f"📦 Received {len(recommendations)} recommendations.")
    if recommendations:
        rec_id = recommendations[0][0] # track_id
        LOG.info(f"👉 Recommended: {rec_id}")
        
        if rec_id in autoplay._engine._bootstrap_manager._bootstrapped_tracks:
            LOG.info("✅ SUCCESS: Recommendation came from Bootstrapped Pool!")
        else:
            LOG.warning(f"❌ FAILURE: Recommendation {rec_id} not in bootstrap pool.")
    else:
        LOG.warning("⚠️ No recommendations returned.")

    # --- TEST CASE 2: Mature System (>= 200 cached) ---
    LOG.info("\n🧪 TEST CASE 2: Mature System (>= 200 cached tracks)")
    
    # Mock cache size
    autoplay._engine._cache._enrichment_cache = {str(i): i for i in range(250)}
    LOG.info(f"📊 Mocked Cache Size: {len(autoplay._engine._cache._enrichment_cache)}")
    
    # Mock Collaborative Matrix
    seed_id = "Ed Sheeran::Shape of You"
    fake_similar = "Rick Astley::Never Gonna Give You Up"
    
    # Mock similar_tracks
    autoplay._engine._collaborative.similar_tracks = MagicMock(return_value=[(fake_similar, 0.99)])
    
    # Mock Last.fm key to pass check
    autoplay._lastfm_key = "dummy_key"
    
    # Mock internal fetch methods to return empty (so we isolate collaborative)
    async def mock_empty(*args, **kwargs): return []
    autoplay._fetch_hot_pool = mock_empty
    autoplay._fetch_warm_start_pools = mock_empty
    autoplay._fetch_cold_start_pools = mock_empty
    autoplay._fetch_entity_based_pools = mock_empty
    
    # Invoke
    recommendations = await autoplay.get_recommendations_for_track(seed_track)
    
    LOG.info(f"📦 Received {len(recommendations)} recommendations.")
    found_collaborative = False
    for track_id, _ in recommendations:
        if track_id == fake_similar:
            found_collaborative = True
            break
            
    if found_collaborative:
        LOG.info("✅ SUCCESS: Recommendation included Collaborative Matrix candidate!")
    else:
        LOG.warning("❌ FAILURE: Collaborative candidate missing.")

if __name__ == "__main__":
    asyncio.run(main())
