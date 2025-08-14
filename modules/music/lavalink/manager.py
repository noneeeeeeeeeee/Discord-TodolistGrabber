import os
import asyncio
import socket
import subprocess
import sys
import time
from typing import Optional
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LL_DIR = BASE_DIR  # modules/music/lavalink
JAR_NAME = "Lavalink.jar"
APP_YML = "application.yml"

DL_URL = os.getenv(
    "LAVALINK_DOWNLOAD_URL",
    "https://github.com/lavalink-devs/Lavalink/releases/latest/download/Lavalink.jar",
)
JAVA = os.getenv("LAVALINK_JAVA_PATH", "java")


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _write_application_yml(host: str, port: int, password: str, secure: bool) -> None:
    yml = f"""server:
  port: {port}
  address: 0.0.0.0

lavalink:
  server:
    password: "{password}"
    sources:
      youtube: true
      bandcamp: true
      soundcloud: true
      twitch: true
      vimeo: true
      http: true
      local: false
    bufferDurationMs: 400
    frameBufferDurationMs: 5000
    resamplingQuality: HIGH
    trackStuckThresholdMs: 10000
    useSeekGhosting: true
    youtubeConfig:
      - allowSearch: true
    plugins: []
"""
    with open(os.path.join(LL_DIR, APP_YML), "w", encoding="utf-8") as f:
        f.write(yml)


def _download_lavalink_jar(path: str) -> None:
    os.makedirs(LL_DIR, exist_ok=True)
    with urllib.request.urlopen(DL_URL, timeout=60) as r, open(path, "wb") as f:
        f.write(r.read())


async def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        if _is_port_open(host, port):
            return True
        await asyncio.sleep(1)
    return False


async def ensure_local_node(host: str, port: int, password: str, secure: bool) -> bool:
    """
    Ensure a Lavalink.jar is present and running on (host, port).
    Returns True if a node is running (started or already available).
    """
    # If already running, done
    if _is_port_open(host, port):
        return True

    os.makedirs(LL_DIR, exist_ok=True)
    jar_path = os.path.join(LL_DIR, JAR_NAME)
    if not os.path.exists(jar_path):
        _download_lavalink_jar(jar_path)

    _write_application_yml(host, port, password, secure)

    # Start Lavalink
    creationflags = 0
    start_kwargs = {}
    if os.name == "nt":
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
        start_kwargs["creationflags"] = creationflags
        start_kwargs["close_fds"] = True

    # Launch process detached
    subprocess.Popen(
        [JAVA, "-jar", JAR_NAME],
        cwd=LL_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **start_kwargs,
    )

    # Wait for port to open
    ok = await _wait_for_port(host, port, timeout=45.0)
    return ok
