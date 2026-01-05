"""
V3 Autoplay Engine - Cache Manager

Sharded JSON storage with atomic writes, crash recovery, integrity checking,
and version migration. Provides persistent storage for song metadata and
ID mappings across Discord sessions.

Architecture:
- Sharded by song ID hash for parallelism
- Atomic file writes via temp file + rename pattern
- CRC32 checksums for integrity verification
- Version-tagged schemas for migrations
- Event-driven notifications on cache operations
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import time
import zlib
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .constants import (
    CacheConfig,
    CacheType,
    EventType,
    V3Config,
    SongMetadata,
    PhysicsLayer,
    SemanticsLayer,
    LibrarianLayer,
)
from .event_bus import EventBus, EventPayload

logger = logging.getLogger(__name__)


class CacheManager:
    """
    Manages sharded persistent cache for V3 autoplay engine.
    
    Uses content-hashed sharding to distribute cache entries across
    multiple files for improved concurrency and reduced file sizes.
    
    Directory Structure:
        ./cache/Autoplay/v3/
            mappings/          # ID cross-reference tables
                shard_00.json
                shard_01.json
                ...
            metadata/          # Full song metadata with analysis
                shard_00.json
                shard_01.json
                ...
            recovery/          # Backup files for crash recovery
            version.json       # Schema version tracking
    """
    
    SCHEMA_VERSION = 1
    SHARD_COUNT = 16  # Number of shards for parallel access
    
    def __init__(
        self,
        config: Optional[CacheConfig] = None,
        event_bus: Optional[EventBus] = None
    ):
        """
        Initialize cache manager with configuration.
        
        Args:
            config: Cache configuration, uses defaults if None
            event_bus: Event bus for notifications, uses global if None
        """
        self.config = config or CacheConfig()
        self.event_bus = event_bus or EventBus()
        
        # Set up paths - use base_path directly from CacheConfig
        self.base_path = Path(self.config.base_path)
        self.mappings_path = self.base_path / "mappings"
        self.metadata_path = self.base_path / "metadata"
        self.recovery_path = self.base_path / "recovery"
        self.version_file = self.base_path / "version.json"
        
        # In-memory cache for hot data
        self._memory_cache: dict[str, dict[str, Any]] = {
            "mappings": {},
            "metadata": {}
        }
        
        # Lock for thread-safe operations
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        
        # Track dirty shards for batch writes
        self._dirty_shards: dict[str, set[int]] = {
            "mappings": set(),
            "metadata": set()
        }
        
        # Statistics
        self._stats = {
            "hits": 0,
            "misses": 0,
            "writes": 0,
            "errors": 0
        }
        
        self._initialized = False
    
    async def initialize(self) -> None:
        """
        Initialize cache directories and load existing data.
        
        Creates directory structure if needed, performs crash recovery,
        runs schema migrations, and loads existing cache into memory.
        """
        if self._initialized:
            return
        
        async with self._global_lock:
            if self._initialized:
                return
            
            logger.info(f"Initializing cache at {self.base_path}")
            
            # Create directory structure
            self._create_directories()
            
            # Check for crash recovery
            await self._perform_crash_recovery()
            
            # Check and migrate schema version
            await self._check_schema_version()
            
            # Load existing cache into memory
            await self._load_all_shards()
            
            # Initialize per-shard locks
            for i in range(self.SHARD_COUNT):
                self._locks[f"mappings_{i}"] = asyncio.Lock()
                self._locks[f"metadata_{i}"] = asyncio.Lock()
            
            self._initialized = True
            logger.info(
                f"Cache initialized: {len(self._memory_cache['metadata'])} metadata entries, "
                f"{len(self._memory_cache['mappings'])} mappings"
            )
            
            # Publish initialization event
            await self.event_bus.publish(EventPayload(
                event_type=EventType.CACHE_LOADED,
                data={
                    "metadata_count": len(self._memory_cache["metadata"]),
                    "mappings_count": len(self._memory_cache["mappings"])
                }
            ))
    
    def _create_directories(self) -> None:
        """Create required directory structure."""
        for path in [self.mappings_path, self.metadata_path, self.recovery_path]:
            path.mkdir(parents=True, exist_ok=True)
    
    async def _perform_crash_recovery(self) -> None:
        """
        Check for and recover from interrupted writes.
        
        Looks for temporary files (.tmp) that indicate interrupted atomic
        writes and attempts to restore from backup files if available.
        """
        # Look for orphaned temp files
        for cache_type in ["mappings", "metadata"]:
            cache_path = self.mappings_path if cache_type == "mappings" else self.metadata_path
            
            for tmp_file in cache_path.glob("*.tmp"):
                logger.warning(f"Found orphaned temp file: {tmp_file}")
                
                # Check for corresponding backup
                backup_file = self.recovery_path / tmp_file.name.replace(".tmp", ".bak")
                target_file = cache_path / tmp_file.name.replace(".tmp", "")
                
                if backup_file.exists():
                    # Restore from backup
                    logger.info(f"Restoring from backup: {backup_file}")
                    shutil.copy2(backup_file, target_file)
                    backup_file.unlink()
                
                # Remove orphaned temp file
                tmp_file.unlink()
                logger.info(f"Cleaned up temp file: {tmp_file}")
    
    async def _check_schema_version(self) -> None:
        """
        Check schema version and run migrations if needed.
        
        Creates version file if not exists, otherwise checks version
        and runs appropriate migration functions.
        """
        if not self.version_file.exists():
            # First run, create version file
            await self._write_version_file()
            return
        
        try:
            with open(self.version_file, "r", encoding="utf-8") as f:
                version_data = json.load(f)
            
            current_version = version_data.get("schema_version", 0)
            
            if current_version < self.SCHEMA_VERSION:
                logger.info(f"Migrating schema from v{current_version} to v{self.SCHEMA_VERSION}")
                await self._migrate_schema(current_version)
                await self._write_version_file()
            
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Error reading version file: {e}")
            await self._write_version_file()
    
    async def _write_version_file(self) -> None:
        """Write current schema version to version file."""
        version_data = {
            "schema_version": self.SCHEMA_VERSION,
            "last_updated": time.time(),
            "shard_count": self.SHARD_COUNT
        }
        
        await self._atomic_write(self.version_file, version_data)
    
    async def _migrate_schema(self, from_version: int) -> None:
        """
        Run schema migrations from given version to current.
        
        Args:
            from_version: Source schema version to migrate from
        """
        # Migration functions for each version bump
        migrations = {
            # 0 -> 1: Initial schema, no migration needed
        }
        
        for version in range(from_version, self.SCHEMA_VERSION):
            if version in migrations:
                logger.info(f"Running migration v{version} -> v{version + 1}")
                await migrations[version]()
    
    async def _load_all_shards(self) -> None:
        """Load all cache shards into memory."""
        tasks = []
        
        for cache_type in ["mappings", "metadata"]:
            cache_path = self.mappings_path if cache_type == "mappings" else self.metadata_path
            
            for i in range(self.SHARD_COUNT):
                shard_file = cache_path / f"shard_{i:02d}.json"
                if shard_file.exists():
                    tasks.append(self._load_shard(cache_type, i, shard_file))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _load_shard(
        self,
        cache_type: str,
        shard_index: int,
        shard_file: Path
    ) -> None:
        """
        Load a single shard from disk into memory.
        
        Args:
            cache_type: Type of cache (mappings or metadata)
            shard_index: Index of the shard
            shard_file: Path to the shard file
        """
        try:
            with open(shard_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Verify checksum if present
            if "_checksum" in data:
                stored_checksum = data.pop("_checksum")
                calculated_checksum = self._calculate_checksum(data)
                
                if stored_checksum != calculated_checksum:
                    logger.error(
                        f"Checksum mismatch in {shard_file}: "
                        f"stored={stored_checksum}, calculated={calculated_checksum}"
                    )
                    self._stats["errors"] += 1
                    return
            
            # Merge into memory cache
            entries = data.get("entries", {})
            self._memory_cache[cache_type].update(entries)
            
            logger.debug(f"Loaded {len(entries)} entries from {shard_file}")
            
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error loading shard {shard_file}: {e}")
            self._stats["errors"] += 1
    
    def _calculate_checksum(self, data: dict) -> str:
        """
        Calculate CRC32 checksum for data integrity verification.
        
        Args:
            data: Dictionary to checksum
            
        Returns:
            Hex string of CRC32 checksum
        """
        json_bytes = json.dumps(data, sort_keys=True).encode("utf-8")
        return format(zlib.crc32(json_bytes) & 0xffffffff, "08x")
    
    def _get_shard_index(self, key: str) -> int:
        """
        Determine shard index for a given key using consistent hashing.
        
        Args:
            key: Cache key to hash
            
        Returns:
            Shard index (0 to SHARD_COUNT-1)
        """
        hash_bytes = hashlib.md5(key.encode("utf-8")).digest()
        hash_int = int.from_bytes(hash_bytes[:4], "big")
        return hash_int % self.SHARD_COUNT
    
    async def _atomic_write(self, file_path: Path, data: dict) -> None:
        """
        Write data to file atomically using temp file + rename.
        
        Args:
            file_path: Target file path
            data: Data to write
        """
        temp_path = file_path.with_suffix(".tmp")
        backup_path = self.recovery_path / f"{file_path.name}.bak"
        
        try:
            # Create backup of existing file
            if file_path.exists():
                shutil.copy2(file_path, backup_path)
            
            # Write to temp file
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            
            # Atomic rename (on Windows, need to remove target first)
            if os.name == "nt" and file_path.exists():
                file_path.unlink()
            
            temp_path.rename(file_path)
            
            # Remove backup on success
            if backup_path.exists():
                backup_path.unlink()
            
            self._stats["writes"] += 1
            
        except IOError as e:
            logger.error(f"Error writing to {file_path}: {e}")
            
            # Attempt to restore from backup
            if backup_path.exists():
                shutil.copy2(backup_path, file_path)
            
            # Clean up temp file
            if temp_path.exists():
                temp_path.unlink()
            
            self._stats["errors"] += 1
            raise
    
    async def get_metadata(self, song_id: str) -> Optional[SongMetadata]:
        """
        Retrieve song metadata from cache.
        
        Args:
            song_id: Unique song identifier (Deezer ID preferred)
            
        Returns:
            SongMetadata if found, None otherwise
        """
        await self.initialize()
        
        data = self._memory_cache["metadata"].get(song_id)
        
        if data is not None:
            self._stats["hits"] += 1
            return self._dict_to_metadata(data)
        
        self._stats["misses"] += 1
        return None
    
    async def set_metadata(self, song_id: str, metadata: SongMetadata) -> None:
        """
        Store song metadata in cache.
        
        Args:
            song_id: Unique song identifier
            metadata: Song metadata to store
        """
        await self.initialize()
        
        shard_index = self._get_shard_index(song_id)
        lock_key = f"metadata_{shard_index}"
        
        async with self._locks[lock_key]:
            # Store in memory
            data = self._metadata_to_dict(metadata)
            self._memory_cache["metadata"][song_id] = data
            
            # Mark shard as dirty
            self._dirty_shards["metadata"].add(shard_index)
        
        # Publish event
        await self.event_bus.publish(EventPayload(
            event_type=EventType.CACHE_UPDATED,
            data={
                "cache_type": "metadata",
                "song_id": song_id,
                "has_physics": metadata.audio_features is not None,
                "has_semantics": metadata.semantic_features is not None,
                "has_librarian": metadata.librarian_info is not None
            }
        ))
    
    async def get_mapping(self, source_id: str) -> Optional[dict[str, str]]:
        """
        Retrieve ID mappings for a song.
        
        Args:
            source_id: Source platform song ID
            
        Returns:
            Dictionary of platform -> ID mappings, None if not found
        """
        await self.initialize()
        
        data = self._memory_cache["mappings"].get(source_id)
        
        if data is not None:
            self._stats["hits"] += 1
            return data
        
        self._stats["misses"] += 1
        return None
    
    async def set_mapping(
        self,
        deezer_id: Optional[str] = None,
        youtube_id: Optional[str] = None,
        lastfm_id: Optional[str] = None,
        isrc: Optional[str] = None
    ) -> None:
        """
        Store ID mapping between platforms.
        
        Creates bidirectional mappings so lookup works from any platform ID.
        
        Args:
            deezer_id: Deezer track ID
            youtube_id: YouTube video ID
            lastfm_id: Last.fm track mbid
            isrc: International Standard Recording Code
        """
        await self.initialize()
        
        mapping = {
            "deezer_id": deezer_id,
            "youtube_id": youtube_id,
            "lastfm_id": lastfm_id,
            "isrc": isrc,
            "updated_at": time.time()
        }
        
        # Remove None values
        mapping = {k: v for k, v in mapping.items() if v is not None}
        
        # Store mapping under each available ID
        ids_to_store = [
            ("deezer", deezer_id),
            ("youtube", youtube_id),
            ("lastfm", lastfm_id),
            ("isrc", isrc)
        ]
        
        for platform, platform_id in ids_to_store:
            if platform_id:
                key = f"{platform}:{platform_id}"
                shard_index = self._get_shard_index(key)
                lock_key = f"mappings_{shard_index}"
                
                async with self._locks[lock_key]:
                    self._memory_cache["mappings"][key] = mapping
                    self._dirty_shards["mappings"].add(shard_index)
        
        # Publish event
        await self.event_bus.publish(EventPayload(
            event_type=EventType.CACHE_UPDATED,
            data={
                "cache_type": "mappings",
                "platforms": [p for p, id_ in ids_to_store if id_]
            }
        ))
    
    async def flush(self) -> None:
        """
        Flush all dirty shards to disk.
        
        Should be called periodically or before shutdown to persist
        in-memory changes.
        """
        await self.initialize()
        
        tasks = []
        
        for cache_type in ["mappings", "metadata"]:
            dirty = self._dirty_shards[cache_type].copy()
            self._dirty_shards[cache_type].clear()
            
            for shard_index in dirty:
                tasks.append(self._flush_shard(cache_type, shard_index))
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            logger.debug(f"Flushed {len(tasks)} dirty shards")
    
    async def _flush_shard(self, cache_type: str, shard_index: int) -> None:
        """
        Flush a single shard to disk.
        
        Args:
            cache_type: Type of cache (mappings or metadata)
            shard_index: Index of the shard to flush
        """
        cache_path = self.mappings_path if cache_type == "mappings" else self.metadata_path
        shard_file = cache_path / f"shard_{shard_index:02d}.json"
        
        lock_key = f"{cache_type}_{shard_index}"
        
        async with self._locks[lock_key]:
            # Collect entries for this shard
            entries = {
                key: value
                for key, value in self._memory_cache[cache_type].items()
                if self._get_shard_index(key) == shard_index
            }
            
            if not entries:
                return
            
            # Build shard data with checksum
            shard_data = {
                "schema_version": self.SCHEMA_VERSION,
                "shard_index": shard_index,
                "entry_count": len(entries),
                "updated_at": time.time(),
                "entries": entries
            }
            
            shard_data["_checksum"] = self._calculate_checksum(
                {k: v for k, v in shard_data.items() if k != "_checksum"}
            )
            
            await self._atomic_write(shard_file, shard_data)
    
    async def clear(self, cache_type: Optional[CacheType] = None) -> None:
        """
        Clear cache data.
        
        Args:
            cache_type: Specific cache type to clear, or None for all
        """
        await self.initialize()
        
        async with self._global_lock:
            if cache_type is None or cache_type == CacheType.MAPPINGS:
                self._memory_cache["mappings"].clear()
                self._dirty_shards["mappings"].clear()
                
                # Remove shard files
                for shard_file in self.mappings_path.glob("shard_*.json"):
                    shard_file.unlink()
            
            if cache_type is None or cache_type == CacheType.METADATA:
                self._memory_cache["metadata"].clear()
                self._dirty_shards["metadata"].clear()
                
                for shard_file in self.metadata_path.glob("shard_*.json"):
                    shard_file.unlink()
            
            logger.info(f"Cleared cache: {cache_type or 'all'}")
    
    async def get_stats(self) -> dict[str, Any]:
        """
        Get cache statistics.
        
        Returns:
            Dictionary with hit rate, entry counts, and error stats
        """
        await self.initialize()
        
        total_requests = self._stats["hits"] + self._stats["misses"]
        hit_rate = self._stats["hits"] / total_requests if total_requests > 0 else 0
        
        return {
            "hit_rate": hit_rate,
            "hits": self._stats["hits"],
            "misses": self._stats["misses"],
            "writes": self._stats["writes"],
            "errors": self._stats["errors"],
            "metadata_count": len(self._memory_cache["metadata"]),
            "mappings_count": len(self._memory_cache["mappings"]),
            "dirty_shards": sum(len(s) for s in self._dirty_shards.values())
        }
    
    async def get_analyzed_song_count(self) -> int:
        """
        Get count of fully analyzed songs in cache.
        
        A song is considered fully analyzed if it has all three
        analysis layers: physics (audio), semantics, and librarian.
        
        Returns:
            Number of fully analyzed songs
        """
        await self.initialize()
        
        count = 0
        for entry in self._memory_cache["metadata"].values():
            if (
                entry.get("audio_features") and
                entry.get("semantic_features") and
                entry.get("librarian_info")
            ):
                count += 1
        
        return count
    
    async def search_by_features(
        self,
        bpm_range: Optional[tuple[float, float]] = None,
        key: Optional[str] = None,
        genres: Optional[list[str]] = None,
        limit: int = 100
    ) -> list[SongMetadata]:
        """
        Search cache by audio features for recommendation filtering.
        
        Args:
            bpm_range: Tuple of (min_bpm, max_bpm)
            key: Musical key to match
            genres: List of genres to match (any)
            limit: Maximum results to return
            
        Returns:
            List of matching SongMetadata objects
        """
        await self.initialize()
        
        results = []
        
        for entry in self._memory_cache["metadata"].values():
            if len(results) >= limit:
                break
            
            audio = entry.get("audio_features", {})
            librarian = entry.get("librarian_info", {})
            
            # Check BPM range
            if bpm_range:
                entry_bpm = audio.get("bpm", 0)
                if not (bpm_range[0] <= entry_bpm <= bpm_range[1]):
                    continue
            
            # Check key
            if key and audio.get("key") != key:
                continue
            
            # Check genres (any match)
            if genres:
                entry_genres = librarian.get("genres", [])
                if not any(g in entry_genres for g in genres):
                    continue
            
            results.append(self._dict_to_metadata(entry))
        
        return results
    
    def _metadata_to_dict(self, metadata: SongMetadata) -> dict:
        """Convert SongMetadata dataclass to dictionary for storage."""
        data = {
            "song_id": metadata.song_id,
            "title": metadata.title,
            "artist": metadata.artist,
            "album": metadata.album,
            "duration_ms": metadata.duration_ms,
            "preview_url": metadata.preview_url,
            "isrc": metadata.isrc,
            "analysis_version": metadata.analysis_version,
            "created_at": metadata.created_at,
            "updated_at": metadata.updated_at
        }
        
        if metadata.audio_features:
            data["audio_features"] = asdict(metadata.audio_features)
        
        if metadata.semantic_features:
            data["semantic_features"] = asdict(metadata.semantic_features)
        
        if metadata.librarian_info:
            data["librarian_info"] = asdict(metadata.librarian_info)
        
        return data
    
    def _dict_to_metadata(self, data: dict) -> SongMetadata:
        """Convert stored dictionary back to SongMetadata dataclass."""
        audio_features = None
        if data.get("audio_features"):
            audio_features = PhysicsLayer(**data["audio_features"])
        
        semantic_features = None
        if data.get("semantic_features"):
            semantic_features = SemanticsLayer(**data["semantic_features"])
        
        librarian_info = None
        if data.get("librarian_info"):
            librarian_info = LibrarianLayer(**data["librarian_info"])
        
        return SongMetadata(
            song_id=data["song_id"],
            title=data["title"],
            artist=data["artist"],
            album=data.get("album"),
            duration_ms=data.get("duration_ms"),
            preview_url=data.get("preview_url"),
            isrc=data.get("isrc"),
            audio_features=audio_features,
            semantic_features=semantic_features,
            librarian_info=librarian_info,
            analysis_version=data.get("analysis_version"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at")
        )
    
    async def shutdown(self) -> None:
        """
        Graceful shutdown: flush all dirty shards and clean up.
        """
        if not self._initialized:
            return
        
        logger.info("Shutting down cache manager...")
        
        # Flush all pending changes
        await self.flush()
        
        # Clear memory cache
        self._memory_cache["mappings"].clear()
        self._memory_cache["metadata"].clear()
        
        self._initialized = False
        logger.info("Cache manager shutdown complete")


# Singleton instance for global access
_cache_manager: Optional[CacheManager] = None


def get_cache_manager() -> CacheManager:
    """
    Get global cache manager instance.
    
    Returns:
        Singleton CacheManager instance
    """
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = CacheManager()
    return _cache_manager
