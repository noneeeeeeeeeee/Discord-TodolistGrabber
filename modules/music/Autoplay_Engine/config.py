"""
Autoplay Version Configuration

Global autoplay engine version setting (V1 or V2).
Set by bot owner in this file - changes require bot restart.
"""

import logging
from typing import Literal, Optional

LOG = logging.getLogger(__name__)

AutoplayVersion = Literal["v1", "v2", "v3"]

# ============================================================================
# CONFIGURATION: Set your autoplay engine version here
# ============================================================================
# "v1" - Legacy engine (stable, no feedback buttons)
# "v2" - New engine with adaptive feedback system (More Like This / Less Like This buttons)
AUTOPLAY_VERSION: AutoplayVersion = "v2"
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
            "v1", "v2", or "v3"
        """
        return self._version
    
    def supports_feedback_buttons(self) -> bool:
        """
        Check if current version supports More/Less Like This buttons
        
        Returns:
            True for V2+, False for V1
        """
        return self._version in ["v2", "v3"]


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
        LastFMAutoplay (V1) or LastFMAutoplayV2 instance
    """
    config = get_autoplay_config()
    version = config.get_autoplay_version()
    
    if version in ("v2"):
        try:
            from .v2 import get_lastfm_autoplay_v2

            return get_lastfm_autoplay_v2(bot)
        except Exception:  # pragma: no cover - defensive fallback
            LOG.exception(
                "Autoplay V2 failed to initialize, falling back to legacy engine"
            )

    from .v1.autoplayengine_v1 import get_lastfm_autoplay

    return get_lastfm_autoplay(bot)

