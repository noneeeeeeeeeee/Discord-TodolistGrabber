"""
Autoplay Version Configuration

Global autoplay engine version setting.
Set by bot owner in this file - changes require bot restart.
"""

import logging
from typing import Literal, Optional

LOG = logging.getLogger(__name__)

AutoplayVersion = Literal["v1", "v3"]

# ============================================================================
# CONFIGURATION: Set your autoplay engine version here
# ============================================================================
# "v1" - Legacy engine (stable, no feedback buttons)
# "v3" - Deezer-native ingest with EfficientAT MobileNet analyzer and adaptive feedback buttons
AUTOPLAY_VERSION: AutoplayVersion = "v3"
# ============================================================================


class AutoplayConfig:
    """Manages global autoplay version configuration"""
    
    def __init__(self):
        self._version: AutoplayVersion = AUTOPLAY_VERSION
        LOG.info(f"🎵 Autoplay Engine: Using version {self._version.upper()}")
    
    def get_autoplay_version(self) -> AutoplayVersion:
        """
        Get configured autoplay version (global setting)
        
        Returns:
            "v1" or "v3"
        """
        return self._version
    
    def supports_feedback_buttons(self) -> bool:
        """
        Check if current version supports More/Less Like This buttons
        
        Returns:
            True for V3, False for V1
        """
        return self._version == "v3"


# Global instance
_autoplay_config: Optional[AutoplayConfig] = None


def get_autoplay_config() -> AutoplayConfig:
    """Get or create global autoplay config instance"""
    global _autoplay_config
    if _autoplay_config is None:
        _autoplay_config = AutoplayConfig()
    return _autoplay_config


def get_autoplay_engine(bot):
    """
    Get the appropriate autoplay engine based on global configuration
    
    Returns:
        LastFMAutoplay (V1) or LastFMAutoplayV3 instance
    """
    config = get_autoplay_config()
    version = config.get_autoplay_version()
    
    if version == "v3":
        try:
            from .v3 import get_lastfm_autoplay_v3

            return get_lastfm_autoplay_v3(bot)
        except Exception:  # pragma: no cover - defensive fallback
            LOG.exception(
                "Autoplay V3 failed to initialize, falling back to legacy engine"
            )
            version = "v1"

    from .v1.autoplayengine_v1 import get_lastfm_autoplay

    return get_lastfm_autoplay(bot)

