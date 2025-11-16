"""HTTP downloader for Deezer preview clips."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional

import aiohttp

LOG = logging.getLogger(__name__)


class PreviewFetcher:
    """Download 30-second Deezer previews to temporary files."""

    def __init__(
        self,
        cache_dir: Path | str = Path("cache/music/previews"),
        *,
        max_concurrent: int = 4,
        timeout: float = 15.0,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))
        self._timeout = timeout
        self._session = session
        self._owns_session = session is None

    async def close(self) -> None:
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    async def fetch_preview(
        self,
        preview_url: str,
        *,
        track_id: Optional[str] = None,
        expected_duration_ms: Optional[int] = None,
    ) -> Optional[Path]:
        if not preview_url:
            return None

        async with self._semaphore:
            session = self._session or aiohttp.ClientSession()
            if self._owns_session and self._session is None:
                self._session = session

            destination = self._cache_dir / f"{uuid.uuid4().hex}.mp3"
            try:
                timeout = aiohttp.ClientTimeout(total=self._timeout)
                async with session.get(preview_url, timeout=timeout) as resp:
                    if resp.status != 200:
                        LOG.debug("Preview fetch failed (%s): HTTP %d", preview_url, resp.status)
                        return None
                    with destination.open("wb") as handle:
                        async for chunk in resp.content.iter_chunked(65536):
                            handle.write(chunk)

                file_size = destination.stat().st_size if destination.exists() else 0
                if file_size == 0:
                    destination.unlink(missing_ok=True)
                    return None

                if expected_duration_ms:
                    LOG.debug(
                        "🎧 Downloaded preview for %s (%.2fs, %d bytes)",
                        track_id or "unknown",
                        expected_duration_ms / 1000.0,
                        file_size,
                    )
                return destination
            except asyncio.TimeoutError:
                LOG.debug("Preview fetch timeout for %s", preview_url)
            except Exception as exc:
                LOG.debug("Preview fetch error for %s: %s", preview_url, exc)
            finally:
                if destination.exists() and destination.stat().st_size == 0:
                    destination.unlink(missing_ok=True)
            return None

    @staticmethod
    def cleanup(path: Optional[Path]) -> None:
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass


__all__ = ["PreviewFetcher"]
