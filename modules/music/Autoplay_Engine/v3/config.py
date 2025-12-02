"""
V3 Autoplay Engine Configuration Constants

This file contains shared constants to avoid circular imports between
autoplayengine_v3.py and bootstrap_manager.py.
"""

import os

# Priority Queue System (Apple Music-style)
# P1: Active enrichment - user waiting, top candidates only (15-25)
# P2: Buffer building - maintains 5-song ready buffer
# P3: Daydreaming - background research when idle (50 per batch)
PRIORITY_ACTIVE = 1      # P1: User-triggered, immediate need
PRIORITY_BUFFER = 2      # P2: Buffer refill, background
PRIORITY_DAYDREAM = 3    # P3: Proactive caching, lowest priority

# Queue limits
MAX_QUEUE_BACKLOG = 200             # Renamed from DAYDREAM_QUEUE_BACKLOG_LIMIT
DAYDREAM_QUEUE_BACKLOG_LIMIT = 200  # Pause daydreaming if queue > 200
DAYDREAM_BATCH_SIZE = 50            # Songs per daydream batch
BUFFER_TARGET_SIZE = 5              # Apple Music-style 5-song buffer
P1_CANDIDATE_LIMIT = 25             # Max candidates for P1 (user waiting)
P1_TIMEOUT_SECONDS = 10             # Notify user if P1 takes longer

# Enrichment Worker Configuration
PROCESSING_QUEUE_MAX = 200          # Max tracks in enrichment queue
FIRST_RUN_FETCH_COUNT = 200         # Tracks to fetch on first run (Last.fm)
DAYDREAM_INTERVAL_SECONDS = 1800    # 30 minutes between daydream cycles
NEW_RELEASE_CHECK_DAYS = 30         # Days between new release checks

# Gemini Batch Queue Configuration
GEMINI_BATCH_SIZE = 50              # Max tracks per Gemini API call
GEMINI_BATCH_TIMEOUT_SECONDS = 8.0  # Seconds to wait before flushing batch

# Verbosity control for console output
# 0 = Errors only 
# 1 = Overview 
# 2 = In-depth 
DEFAULT_VERBOSITY = int(os.getenv("AUTOPLAY_V3_VERBOSITY", "0"))

# V3 Configuration: Read from environment variables with sensible defaults
DEFAULT_ANALYSIS_MODE = os.getenv("ANALYSIS_MODE", "ml").lower()  # "ml" or "non-ml"
DEFAULT_EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "mn10_as").lower()
MAX_ANALYSIS_RETRIES = 3

# Environment variable names
LASTFM_API_KEY_ENV = "LASTFM_API_KEY"
PARSING_SCHEMA_VERSION = 2
