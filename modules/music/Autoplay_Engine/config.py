"""
Autoplay Version Configuration

V3 autoplay engine - feedback buttons + context-aware recommendations.
V1 support has been removed. A future V3-lite version may be added.
"""

import logging
from typing import Optional

LOG = logging.getLogger(__name__)


class AutoplayConfig:
    """Manages autoplay engine configuration (V3 only)"""
    
    def __init__(self):
        LOG.info("🎵 Autoplay Engine: V3 (feedback + context-aware)")
    
    def supports_feedback_buttons(self) -> bool:
        """
        Check if current version supports More Like This button.
        
        Returns:
            True (V3 always supports feedback)
        """
        return True


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
    Get the V3 autoplay engine.
    
    Returns:
        LastFMAutoplayV3 instance
        
    Note:
        V1 support has been removed. Use V3 only.
        A future V3-lite version may be added for simpler setups.
    """
    from .v3 import get_lastfm_autoplay_v3
    return get_lastfm_autoplay_v3(bot)

