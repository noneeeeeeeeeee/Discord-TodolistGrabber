"""Utilities for automatically provisioning and supervising a Lavalink node."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Optional, Tuple

import aiohttp

__all__ = [
    "LavalinkConfig",
    "LavalinkManager",
    "ensure_local_node",
    "shutdown_local_node",
    "is_local_node_managed",
]


GITHUB_API = "https://api.github.com"
LAVALINK_REPO = "lavalink-devs/Lavalink"
YOUTUBE_PLUGIN_COORD = "dev.lavalink.youtube:youtube-plugin"
SPONSORBLOCK_PLUGIN_COORD = "com.github.topi314.sponsorblock:sponsorblock-plugin"
DEFAULT_LAVALINK_VERSION = "4.1.1"
DEFAULT_YOUTUBE_PLUGIN_VERSION = "1.13.5"
DEFAULT_SPONSORBLOCK_PLUGIN_VERSION = "3.0.1"
LAVALINK_MAJOR = "4"

HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "Discord-TodolistGrabber/1.0",
}


@dataclass(slots=True)
class LavalinkConfig:
    host: str
    port: int
    password: str
    secure: bool = False


class LavalinkManager:
    """Download, configure, start and stop a managed Lavalink process."""

    _instance: ClassVar[Optional["LavalinkManager"]] = None

    def __init__(self) -> None:
        base = Path(__file__).resolve().parent
        self.base_dir = base
        self.jar_path = base / "Lavalink.jar"
        self.config_path = base / "application.yml"
        self.plugins_dir = base / "plugins"
        self.version_path = base / ".lavalink-version.json"
        self._process: Optional[subprocess.Popen] = None
        self._lock = asyncio.Lock()
        self._managed = False

    @classmethod
    def instance(cls) -> "LavalinkManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def ensure_running(self, config: LavalinkConfig) -> bool:
        """Ensure Lavalink is reachable, downloading and starting it if needed."""

        async with self._lock:
            if self._is_process_running() and self._managed:
                if await self._wait_for_port(config.host, config.port, timeout=1.0):
                    return True
                await self._terminate_process()

            if await self._wait_for_port(config.host, config.port, timeout=0.5):
                self._managed = False
                return True

            await self._prepare_assets(config)

            started = await self._start_process()
            if not started:
                return False

            ready = await self._wait_for_port(config.host, config.port, timeout=60.0)
            if not ready:
                await self._terminate_process()
                return False

            self._managed = True
            return True

    async def shutdown(self) -> None:
        """Terminate the managed Lavalink process if we launched it."""

        async with self._lock:
            await self._terminate_process()
            self._managed = False

    async def _prepare_assets(self, config: LavalinkConfig) -> None:
        if config.secure:
            print(
                "[Lavalink] SSL was requested for the local node, but automatic provisioning only "
                "enables plain HTTP. Configure certificates manually if HTTPS is required."
            )

        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        (self.base_dir / "logs").mkdir(parents=True, exist_ok=True)

        async with aiohttp.ClientSession(
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=180),
        ) as session:
            version, url = await self._resolve_lavalink_release(session)
            await self._download_if_needed(session, version, url)

        await self._ensure_config(config)

    async def _resolve_lavalink_release(
        self, session: aiohttp.ClientSession
    ) -> Tuple[str, str]:
        override_url = os.getenv("LAVALINK_JAR_URL")
        override_version = os.getenv("LAVALINK_JAR_VERSION")
        if override_url:
            return override_version or DEFAULT_LAVALINK_VERSION, override_url

        releases_url = f"{GITHUB_API}/repos/{LAVALINK_REPO}/releases?per_page=5"
        try:
            async with session.get(releases_url) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"GitHub API returned {resp.status}")
                data = await resp.json()
        except Exception:
            download = f"https://github.com/{LAVALINK_REPO}/releases/download/{DEFAULT_LAVALINK_VERSION}/Lavalink.jar"
            return DEFAULT_LAVALINK_VERSION, download

        for release in data:
            if release.get("draft") or release.get("prerelease"):
                continue
            tag = (release.get("tag_name") or "").lstrip("v")
            if not tag.startswith(f"{LAVALINK_MAJOR}."):
                continue
            for asset in release.get("assets") or []:
                if asset.get("name") == "Lavalink.jar" and asset.get(
                    "browser_download_url"
                ):
                    return tag, asset["browser_download_url"]

        download = f"https://github.com/{LAVALINK_REPO}/releases/download/{DEFAULT_LAVALINK_VERSION}/Lavalink.jar"
        return DEFAULT_LAVALINK_VERSION, download

    async def _download_if_needed(
        self, session: aiohttp.ClientSession, version: str, url: str
    ) -> None:
        current = self._read_version()
        if current and current.get("version") == version and self.jar_path.exists():
            return

        tmp_path = self.jar_path.with_suffix(".tmp")
        async with session.get(url) as resp:
            resp.raise_for_status()
            with tmp_path.open("wb") as fp:
                async for chunk in resp.content.iter_chunked(1 << 15):
                    fp.write(chunk)
        tmp_path.replace(self.jar_path)
        self._write_version({"version": version, "download_url": url})

    async def _ensure_config(self, config: LavalinkConfig) -> None:
        youtube_version = os.getenv(
            "LAVALINK_YOUTUBE_PLUGIN_VERSION", DEFAULT_YOUTUBE_PLUGIN_VERSION
        )
        sponsor_version = os.getenv(
            "LAVALINK_SPONSORBLOCK_PLUGIN_VERSION", DEFAULT_SPONSORBLOCK_PLUGIN_VERSION
        )

        yaml_payload = {
            "server": {
                "port": config.port,
                "address": "0.0.0.0",
                "ssl": {"enabled": bool(config.secure)},
                "http2": {"enabled": False},
            },
            "lavalink": {
                "plugins": [
                    {
                        "dependency": f"{YOUTUBE_PLUGIN_COORD}:{youtube_version}",
                        "snapshot": False,
                    },
                    {
                        "dependency": f"{SPONSORBLOCK_PLUGIN_COORD}:{sponsor_version}",
                        "snapshot": False,
                    },
                ],
                "pluginsDir": "./plugins",
                "server": {
                    "password": config.password,
                    "sources": {
                        "youtube": False,
                        "bandcamp": True,
                        "soundcloud": True,
                        "twitch": True,
                        "vimeo": True,
                        "nico": True,
                        "http": True,
                        "local": False,
                    },
                    "filters": {
                        "volume": True,
                        "equalizer": True,
                        "karaoke": True,
                        "timescale": True,
                        "tremolo": True,
                        "vibrato": True,
                        "distortion": True,
                        "rotation": True,
                        "channelMix": True,
                        "lowPass": True,
                    },
                    "playerUpdateInterval": 5,
                    "youtubeSearchEnabled": True,
                    "soundcloudSearchEnabled": True,
                    "useSeekGhosting": True,
                },
            },
            "logging": {
                "file": {"path": "./logs/"},
                "level": {"root": "INFO", "lavalink": "INFO"},
            },
        }

        content = self._dump_yaml(yaml_payload)
        existing = (
            self.config_path.read_text(encoding="utf-8")
            if self.config_path.exists()
            else ""
        )
        if existing.strip() != content.strip():
            self.config_path.write_text(content, encoding="utf-8")

    async def _start_process(self) -> bool:
        java_binary = os.getenv("LAVALINK_JAVA_PATH") or "java"
        if not self.jar_path.exists():
            return False

        argv = [
            java_binary,
            "-Djdk.tls.client.protocols=TLSv1.3",
            "-jar",
            str(self.jar_path),
        ]
        env = os.environ.copy()

        start_kwargs = {
            "cwd": str(self.base_dir),
            "env": env,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }

        if os.name == "nt":
            start_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
            )
            start_kwargs["close_fds"] = True
        else:
            start_kwargs["start_new_session"] = True

        try:
            self._process = subprocess.Popen(argv, **start_kwargs)
            return True
        except FileNotFoundError:
            print(
                "[Lavalink] Unable to invoke Java. Please ensure Java 17+ is installed and on PATH, "
                "or set LAVALINK_JAVA_PATH to the JVM executable."
            )
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[Lavalink] Failed to launch Lavalink: {exc}")
        return False

    async def _terminate_process(self) -> None:
        if not self._managed or not self._process:
            if self._process and self._process.poll() is not None:
                self._process = None
            return

        proc = self._process
        if proc and proc.poll() is None:
            if os.name == "nt":
                proc.terminate()
            else:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(proc.wait, 10),
                    timeout=12,
                )
            except (asyncio.TimeoutError, subprocess.TimeoutExpired):
                with contextlib.suppress(Exception):
                    proc.kill()
        self._process = None

    def _is_process_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def is_managed_process(self) -> bool:
        """Return True if the currently running node is managed by this instance."""

        return self._managed and self._is_process_running()

    async def _wait_for_port(self, host: str, port: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._is_port_open(host, port):
                return True
            await asyncio.sleep(1.0)
        return False

    @staticmethod
    def _is_port_open(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return True
        except OSError:
            return False

    def _read_version(self) -> Optional[dict]:
        if not self.version_path.exists():
            return None
        try:
            return json.loads(self.version_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_version(self, payload: dict) -> None:
        try:
            self.version_path.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def _dump_yaml(self, payload: dict) -> str:
        lines: list[str] = []

        def render_scalar(value) -> str:
            if isinstance(value, bool):
                return "true" if value else "false"
            if value is None:
                return "null"
            if isinstance(value, (int, float)):
                return str(value)
            return json.dumps(value)

        def walk(obj, indent: int = 0) -> None:
            prefix = " " * indent
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if isinstance(value, (dict, list)):
                        lines.append(f"{prefix}{key}:")
                        walk(value, indent + 2)
                    else:
                        lines.append(f"{prefix}{key}: {render_scalar(value)}")
            elif isinstance(obj, list):
                for item in obj:
                    if isinstance(item, (dict, list)):
                        lines.append(f"{prefix}-")
                        walk(item, indent + 2)
                    else:
                        lines.append(f"{prefix}- {render_scalar(item)}")
            else:
                lines.append(f"{prefix}{render_scalar(obj)}")

        walk(payload)
        return "\n".join(lines) + "\n"


async def ensure_local_node(host: str, port: int, password: str, secure: bool) -> bool:
    config = LavalinkConfig(host=host, port=port, password=password, secure=secure)
    return await LavalinkManager.instance().ensure_running(config)


async def shutdown_local_node() -> None:
    await LavalinkManager.instance().shutdown()


def is_local_node_managed() -> bool:
    """Helper to check if the Lavalink process is managed by this manager."""

    return LavalinkManager.instance().is_managed_process()
