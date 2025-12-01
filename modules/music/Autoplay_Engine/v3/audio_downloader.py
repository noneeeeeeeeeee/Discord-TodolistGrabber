"""
⚠️ DEPRECATED MODULE - audio_downloader.py

This module is DEPRECATED as of 2025-12-XX.

Reason:
    The Autoplay V3 system now exclusively uses Deezer previews for audio analysis.
    YouTube audio downloading via yt-dlp is no longer needed because:
    
    1. Deezer has ~120 million songs with new releases included
    2. Deezer previews are legal, consistent 30-second clips
    3. If a track isn't on Deezer, we assume it's not commercial music
    4. YouTube requires yt-dlp maintenance and format changes
    
Replacement:
    Use `preview_fetcher.py` for all audio analysis needs.
    
Migration:
    - Remove all calls to AudioDownloader.download_audio()
    - Use PreviewFetcher.fetch_preview() instead
    - If no Deezer preview is available, skip the track (don't fallback to YouTube)
    
This file is kept temporarily for rollback purposes. It will be removed in a future release.
"""
import asyncio
import logging
import tempfile
import warnings
from pathlib import Path
from typing import Optional

from .dependency_manager import wait_for_ffmpeg

LOG = logging.getLogger(__name__)

# Emit deprecation warning when module is imported
warnings.warn(
    "audio_downloader.py is deprecated. Use preview_fetcher.py (Deezer previews) instead.",
    DeprecationWarning,
    stacklevel=2
)

try:  # Lazy import to avoid mandatory dependency during startup
    from yt_dlp import YoutubeDL  # type: ignore
    from yt_dlp.utils import DownloadError, PostProcessingError  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    YoutubeDL = None
    DownloadError = PostProcessingError = Exception  # type: ignore[misc,assignment]


class AudioDownloader:
    """Thin async wrapper around yt-dlp for temporary audio downloads."""

    def __init__(
        self,
        cache_dir: Path | str = Path("cache/music/audio"),
        *,
        max_concurrent: int = 2,
        timeout: float = 120.0,
        preferred_codec: str = "wav",
        preferred_quality: str = "192",
    ) -> None:
        if YoutubeDL is None:
            raise RuntimeError(
                "yt-dlp is not installed. Install it via 'pip install yt-dlp' to enable audio analysis."
            )

        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._timeout = max(30.0, timeout)
        self._preferred_codec = preferred_codec
        self._preferred_quality = preferred_quality
        self._ffmpeg_bin = wait_for_ffmpeg(timeout=10.0)
        self._ffmpeg_reported_ready = self._ffmpeg_bin is not None
        if self._ffmpeg_bin:
            LOG.info("🎛️ [AudioDownloader] Using ffmpeg from %s", self._ffmpeg_bin)
        else:
            LOG.warning(
                "⚠️ [AudioDownloader] ffmpeg not detected yet. yt-dlp may warn until installer finishes."
            )

    async def download_audio(self, youtube_url: str) -> Optional[Path]:
        """Download ``youtube_url`` as a temporary audio file ready for Librosa."""
        if not youtube_url:
            return None

        async with self._semaphore:
            loop = asyncio.get_running_loop()
            temp_dir = Path(
                tempfile.mkdtemp(prefix="autoplay_v2_", dir=self._cache_dir)
            )
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(None, self._download_sync, youtube_url, temp_dir),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                LOG.warning("⏱️ [AudioDownloader] Timed out downloading %s", youtube_url)
            except Exception as exc:
                LOG.warning("❌ [AudioDownloader] Download failed for %s: %s", youtube_url, exc)
            finally:
                # If nothing was saved, clean up the temp directory immediately
                if temp_dir.exists() and not any(temp_dir.iterdir()):
                    temp_dir.rmdir()
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _download_sync(self, youtube_url: str, temp_dir: Path) -> Optional[Path]:
        """Blocking download helper executed inside a thread pool."""
        output_template = temp_dir / "%(id)s.%(ext)s"
        ffmpeg_dir = self._ensure_ffmpeg_ready()
        if not ffmpeg_dir:
            raise RuntimeError(
                "ffmpeg binaries were not detected within the expected window. "
                "Ensure the auto-installer completes or install ffmpeg manually."
            )
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": False,
            "format": "bestaudio/best",
            "outtmpl": str(output_template),
            "prefer_ffmpeg": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": self._preferred_codec,
                    "preferredquality": self._preferred_quality,
                }
            ],
        }
        ydl_opts["ffmpeg_location"] = str(ffmpeg_dir)

        try:
            with YoutubeDL(ydl_opts) as ydl:  # type: ignore[arg-type]
                info = ydl.extract_info(youtube_url, download=True)
                guessed_path = Path(ydl.prepare_filename(info))
        except PostProcessingError as exc:  # type: ignore[misc]
            message = str(exc)
            if "ffmpeg" in message.lower():
                raise RuntimeError(
                    "ffmpeg is required for audio extraction but is still unavailable. "
                    "Wait for the bundled installer to finish or install ffmpeg in PATH."
                ) from exc
            raise
        except DownloadError as exc:  # type: ignore[misc]
            raise RuntimeError(f"yt-dlp download error: {exc}") from exc

        file_path = self._resolve_output_file(temp_dir, guessed_path)
        if not file_path:
            LOG.warning(
                "⚠️ [AudioDownloader] No output file detected after download (url=%s, temp=%s)",
                youtube_url,
                temp_dir,
            )
            return None

        LOG.debug(
            "📥 [AudioDownloader] Downloaded %s -> %s",
            youtube_url,
            file_path.name,
        )
        return file_path

    def _ensure_ffmpeg_ready(self, wait_timeout: Optional[float] = None) -> Optional[Path]:
        """Refresh the cached ffmpeg path, waiting for installer completion if needed."""
        if self._ffmpeg_bin and self._ffmpeg_bin.exists():
            return self._ffmpeg_bin

        timeout_budget = wait_timeout or max(30.0, min(60.0, self._timeout * 0.5))
        ffmpeg_dir = wait_for_ffmpeg(timeout=timeout_budget)
        if ffmpeg_dir:
            self._ffmpeg_bin = ffmpeg_dir
            if not self._ffmpeg_reported_ready:
                LOG.info("🎛️ [AudioDownloader] ffmpeg is now available at %s", ffmpeg_dir)
                self._ffmpeg_reported_ready = True
        return ffmpeg_dir

    def _resolve_output_file(self, temp_dir: Path, guessed_path: Path) -> Optional[Path]:
        """Locate the actual audio file on disk after yt-dlp post-processing."""
        if guessed_path.exists():
            return guessed_path

        preferred = guessed_path.with_suffix(f".{self._preferred_codec}")
        if preferred.exists():
            return preferred

        fallback = next(temp_dir.glob(f"*.{self._preferred_codec}"), None)
        if fallback and fallback.exists():
            return fallback

        any_file = next((path for path in temp_dir.iterdir() if path.is_file()), None)
        return any_file
