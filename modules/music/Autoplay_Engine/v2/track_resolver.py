import asyncio
import logging
import time
from typing import Any, Dict, Optional

from .cache_manager import CacheManager, MappingEntry

LOG = logging.getLogger(__name__)


class TrackResolver:
    """Resolves tracks via Pomice and maintains mapping cache."""

    def __init__(self, cache_manager: CacheManager) -> None:
        self._cache = cache_manager

    async def resolve_track(
        self,
        artist: str,
        title: str,
        *,
        expected_duration_ms: Optional[int] = None,
        prefer_cache: bool = True,
    ) -> Optional[Any]:
        artist = artist.strip()
        title = title.strip()
        if not artist or not title:
            return None

        if prefer_cache:
            mapping = await self._cache.get_mapping(artist, title)
            if mapping:
                rebuilt = await self._create_track_obj_from_mapping(mapping)
                if rebuilt:
                    if LOG.isEnabledFor(logging.DEBUG):
                        LOG.debug(
                            "Track resolved from cache: %s",
                            {
                                "artist": artist,
                                "title": title,
                                "youtube_id": mapping.youtube_id,
                                "channel": mapping.channel_name,
                                "verified": mapping.verified,
                                "source": "cache",
                            },
                        )
                    return rebuilt

        track_obj = await self._search_with_pomice(
            artist,
            title,
            expected_duration_ms=expected_duration_ms,
        )
        if not track_obj:
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug(
                    "Track resolution failed: %s",
                    {
                        "artist": artist,
                        "title": title,
                        "expected_duration_ms": expected_duration_ms,
                    },
                )
            return None

        metadata = self._extract_track_metadata(track_obj)
        youtube_id_val = metadata["youtube_id"]
        url_val = metadata["url"]
        if youtube_id_val and url_val:
            duration_val = metadata.get("duration_ms")
            if expected_duration_ms and duration_val:
                duration_val = int(duration_val)

            entry = MappingEntry(
                youtube_id=str(youtube_id_val),
                url=str(url_val),
                timestamp=time.time(),
                channel_name=metadata.get("channel_name"),
                verified=bool(metadata.get("verified", False)),
                duration_ms=duration_val,
                track_identifier=metadata.get("track_identifier"),
            )
            await self._cache.set_mapping(artist, title, entry)
            if LOG.isEnabledFor(logging.DEBUG):
                LOG.debug(
                    "Track mapping stored: %s",
                    {
                        "artist": artist,
                        "title": title,
                        "youtube_id": entry.youtube_id,
                        "duration_ms": entry.duration_ms,
                        "channel": entry.channel_name,
                        "verified": entry.verified,
                        "source": "search",
                    },
                )
        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug(
                "Track resolved via Pomice: %s",
                {
                    "artist": artist,
                    "title": title,
                    "youtube_id": metadata.get("youtube_id"),
                    "channel": metadata.get("channel_name"),
                    "verified": metadata.get("verified"),
                    "score_duration_ms": metadata.get("duration_ms"),
                },
            )
        return track_obj

    async def validate_pomice_path(self) -> bool:
        node = await self._get_node()
        return node is not None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _create_track_obj_from_mapping(self, mapping: MappingEntry) -> Optional[Any]:
        node = await self._get_node()
        if not node:
            return None

        identifier = mapping.track_identifier
        if identifier:
            builder = getattr(node, "build_track", None)
            if builder:
                try:
                    if asyncio.iscoroutinefunction(builder):
                        track_obj = await builder(identifier)
                    else:
                        track_obj = builder(identifier)
                    if track_obj:
                        return track_obj
                except Exception as exc:
                    LOG.debug("Pomice build_track failed for identifier %s: %s", identifier, exc)

        query = mapping.url or mapping.youtube_id
        if not query:
            return None

        try:
            tracks = await node.get_tracks(query)
        except Exception as exc:
            LOG.debug("Pomice get_tracks failed for query %s: %s", query, exc)
            tracks = []

        if not tracks and mapping.youtube_id:
            try:
                tracks = await node.get_tracks(f"https://www.youtube.com/watch?v={mapping.youtube_id}")
            except Exception:
                tracks = []

        if not tracks:
            return None

        for track_obj in tracks:
            metadata = self._extract_track_metadata(track_obj)
            if metadata["youtube_id"] == mapping.youtube_id:
                return track_obj
        return tracks[0]

    async def _search_with_pomice(
        self,
        artist: str,
        title: str,
        *,
        expected_duration_ms: Optional[int] = None,
    ) -> Optional[Any]:
        node = await self._get_node()
        if not node:
            return None

        query = f"ytsearch:{artist} {title} official audio"
        try:
            results = await node.get_tracks(query)
        except Exception as exc:
            LOG.warning("Pomice search failed for %s: %s", query, exc)
            return None

        if not results:
            LOG.debug("No Pomice results for %s", query)
            return None

        best_track: Optional[Any] = None
        best_score = float("-inf")
        duration_target = expected_duration_ms or 0
        normalized_artist = artist.casefold()
        normalized_title = title.casefold()

        for index, candidate in enumerate(results):
            metadata = self._extract_track_metadata(candidate)
            score = 0.0

            if metadata["verified"]:
                score += 3.0

            channel = (metadata.get("channel_name") or "").casefold()
            if channel and normalized_artist in channel:
                score += 1.5

            candidate_title = (getattr(candidate, "title", None) or metadata.get("title") or "").casefold()
            if candidate_title and normalized_title and normalized_title in candidate_title:
                score += 1.0

            duration_val = metadata.get("duration_ms") or 0
            if duration_target and duration_val:
                delta = abs(duration_target - int(duration_val))
                score -= min(delta / 1000.0, 10.0)

            score -= index * 0.1

            if score > best_score:
                best_score = score
                best_track = candidate

        return best_track or results[0]

    async def _get_node(self) -> Optional[Any]:
        try:
            import pomice  # type: ignore[import-error]
        except Exception as exc:  # pragma: no cover - dependency optional
            LOG.error("Pomice not available: %s", exc)
            return None

        node_pool = getattr(pomice, "NodePool", None)
        if node_pool is None:
            LOG.error("Pomice NodePool not available")
            return None

        try:
            node = node_pool.get_node()
        except Exception as exc:
            LOG.error("Failed to get Pomice node: %s", exc)
            return None

        if not node:
            LOG.error("Pomice NodePool returned no node")
            return None
        return node

    @staticmethod
    def _extract_track_metadata(track_obj: Any) -> Dict[str, Optional[Any]]:
        info = getattr(track_obj, "info", {}) or {}
        youtube_id = info.get("identifier") or getattr(track_obj, "identifier", None)
        url = info.get("uri") or getattr(track_obj, "uri", None)
        channel = info.get("author") or getattr(track_obj, "author", None)
        duration_ms = info.get("length") or getattr(track_obj, "length", None)
        title = info.get("title") or getattr(track_obj, "title", None)
        verified = bool(info.get("isVerified") or info.get("isOfficial"))
        track_identifier = getattr(track_obj, "track_id", None) or getattr(track_obj, "track", None)
        if youtube_id and not url:
            url = f"https://www.youtube.com/watch?v={youtube_id}"

        try:
            duration_int = int(duration_ms) if duration_ms is not None else None
        except (TypeError, ValueError):
            duration_int = None

        if track_identifier is not None:
            track_identifier = str(track_identifier)

        return {
            "youtube_id": youtube_id,
            "url": url,
            "channel_name": channel,
            "verified": verified,
            "duration_ms": duration_int,
            "track_identifier": track_identifier,
            "title": title,
        }


__all__ = ["TrackResolver"]
