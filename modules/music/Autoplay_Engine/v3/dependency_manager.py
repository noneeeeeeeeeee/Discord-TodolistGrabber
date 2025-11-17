"""Unified dependency management for Autoplay V3.

This module provisions ffmpeg binaries (when not already available on the
system PATH) and downloads EfficientAT MobileNet checkpoints used by the new
enriching service. The dependency installer can run in the background so the
bot startup does not block on download latency.
"""
from __future__ import annotations

import argparse
import logging
import os
import platform
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Dict, Optional, Sequence
from urllib.request import urlopen

LOG = logging.getLogger(__name__)

_DEPENDENCY_ROOT = Path(__file__).parent / "dependencies"
_FFMPEG_ROOT = _DEPENDENCY_ROOT / "ffmpeg"
_FFMPEG_BIN = _FFMPEG_ROOT / "bin"
_MODEL_DIR = _DEPENDENCY_ROOT / "models"

_WINDOWS_RELEASE = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
_LINUX_RELEASE = "https://johnvansickle.com/ffmpeg/builds/ffmpeg-release-amd64-static.tar.xz"
_MAC_FFMPEG_RELEASE = "https://evermeet.cx/ffmpeg/ffmpeg-6.1.zip"
_MAC_FFPROBE_RELEASE = "https://evermeet.cx/ffmpeg/ffprobe-6.1.zip"
_DOWNLOAD_TIMEOUT = 15.0

_INSTALL_THREAD: Optional[threading.Thread] = None
_INSTALL_LOCK = threading.Lock()

_MOBILENET_MODELS: Dict[str, Dict[str, str]] = {
    "mn10_as": {
        "filename": "mn10_as_mAP_471.pt",
        "url": "https://github.com/fschmid56/EfficientAT/releases/download/v0.0.1/mn10_as_mAP_471.pt",
        "label": "EfficientAT MobileNetV3 Large (mn10_as, 527 logits)",
        "embedding_model": "mn10_as",
    }
}

_MODEL_BUFFER_SIZE = 1024 * 1024  # 1 MiB


class DependencyManager:
    """High-level orchestration for runtime dependencies."""

    def __init__(self, *, model_key: str = "mn10_as") -> None:
        self._model_key = model_key

    def ensure_ready(self) -> Optional[Path]:
        """Ensure ffmpeg is reachable and the ML checkpoint exists."""
        self.ensure_ffmpeg()
        return ensure_model_file(self._model_key)

    def ensure_ffmpeg(self, *, timeout: float = 30.0) -> Optional[Path]:
        return wait_for_ffmpeg(timeout=timeout)

    def ensure_model(self, *, force: bool = False) -> Optional[Path]:
        return ensure_model_file(self._model_key, force=force)

    def model_directory(self) -> Path:
        return get_model_directory()


def ensure_ffmpeg_binaries() -> Optional[Path]:
    """Ensure ffmpeg + ffprobe binaries exist and return the bin directory."""
    path = _detect_existing_in_path()
    if path:
        return path

    path = _detect_local_bundle()
    if path:
        return path

    _start_background_install()
    return None


def wait_for_ffmpeg(timeout: float = 15.0, interval: float = 0.5) -> Optional[Path]:
    """Wait up to ``timeout`` seconds for ffmpeg binaries to become available."""
    start = time.monotonic()
    path = get_ffmpeg_bin_dir()
    if path:
        return path

    ensure_ffmpeg_binaries()
    while time.monotonic() - start < timeout:
        path = get_ffmpeg_bin_dir()
        if path:
            return path
        time.sleep(interval)
    LOG.debug("⚠️ ffmpeg bundle not ready after %.1fs", timeout)
    return None


def get_ffmpeg_bin_dir() -> Optional[Path]:
    """Return the ffmpeg bin directory if already available."""
    return _detect_existing_in_path() or _detect_local_bundle()


def ensure_model_file(model_key: str, *, force: bool = False) -> Optional[Path]:
    """Ensure a MobileNet checkpoint exists locally and return the path."""
    info = _MOBILENET_MODELS.get(model_key)
    if not info:
        LOG.error("Unknown model key '%s'", model_key)
        return None

    destination = get_model_directory() / info["filename"]
    if destination.exists() and not force:
        return destination

    tmp_path = destination.with_suffix(destination.suffix + ".partial")
    try:
        _download_file(info["url"], tmp_path)
        tmp_path.replace(destination)
        LOG.info("✅ Saved MobileNet checkpoint to %s", destination)
        return destination
    except Exception as exc:  # pragma: no cover - network I/O
        LOG.warning("Failed to download %s: %s", info["url"], exc)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        return None


def get_model_directory() -> Path:
    """Return the directory where ML checkpoints are stored."""
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    return _MODEL_DIR


def list_available_models() -> Dict[str, Dict[str, str]]:
    """Return a copy of the supported EfficientAT MobileNet definitions."""
    return {key: dict(info) for key, info in _MOBILENET_MODELS.items()}


def download_mobilenet_checkpoint(
    model_key: str,
    *,
    force: bool = False,
    show_progress: bool = True,
) -> Path:
    """Download a MobileNet checkpoint with optional progress output."""
    info = _MOBILENET_MODELS.get(model_key)
    if not info:
        raise ValueError(f"Unknown model key '{model_key}'")

    destination = get_model_directory() / info["filename"]
    if destination.exists() and not force:
        if show_progress:
            print(f"✅ {destination.name} already exists. Use --force to re-download.")
        return destination

    tmp_path = destination.with_suffix(destination.suffix + ".partial")
    downloaded = 0
    total = 0
    try:
        with urlopen(info["url"], timeout=_DOWNLOAD_TIMEOUT) as response, open(
            tmp_path, "wb"
        ) as fh:
            length = response.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else 0
            while True:
                chunk = response.read(_MODEL_BUFFER_SIZE)
                if not chunk:
                    break
                fh.write(chunk)
                downloaded += len(chunk)
                if show_progress and total:
                    percent = downloaded / total * 100
                    print(
                        f"\r⬇️  Downloading {destination.name}: {percent:5.1f}%",
                        end="",
                        flush=True,
                    )
        if show_progress and total:
            print()
        tmp_path.replace(destination)
    except Exception as exc:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise RuntimeError(f"Failed to download {info['filename']}: {exc}") from exc

    LOG.info("✅ Saved MobileNet checkpoint to %s", destination)
    return destination


def _repo_root() -> Path:
    """Return the repository root to locate the .env file."""
    return Path(__file__).resolve().parents[4]


def update_embedding_model_env(
    embedding_model: str,
    *,
    env_path: Optional[Path] = None,
) -> bool:
    """Update (or append) the EMBEDDING_MODEL entry inside .env."""
    env_file = env_path or (_repo_root() / ".env")
    if not env_file.exists():
        LOG.warning("⚠️ .env file not found at %s", env_file)
        return False

    lines = env_file.read_text().splitlines()
    target_line = f"EMBEDDING_MODEL={embedding_model}"
    for idx, line in enumerate(lines):
        if line.strip().startswith("EMBEDDING_MODEL="):
            lines[idx] = target_line
            break
    else:
        lines.append(target_line)

    env_file.write_text("\n".join(lines) + "\n")
    LOG.info("📝 Updated %s -> %s", env_file.name, target_line)
    return True

def _detect_existing_in_path() -> Optional[Path]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        LOG.debug("✅ System ffmpeg detected at %s", ffmpeg)
        return Path(ffmpeg).resolve().parent
    return None


def _detect_local_bundle() -> Optional[Path]:
    ffmpeg_bin = _FFMPEG_BIN
    suffix = ".exe" if os.name == "nt" else ""
    ffmpeg_path = ffmpeg_bin / f"ffmpeg{suffix}"
    ffprobe_path = ffmpeg_bin / f"ffprobe{suffix}"
    if ffmpeg_path.exists() and ffprobe_path.exists():
        _prepend_path(ffmpeg_bin)
        LOG.info("🎧 Using bundled ffmpeg binaries from %s", ffmpeg_bin)
        return ffmpeg_bin
    return None


def _prepend_path(bin_dir: Path) -> None:
    path_value = os.environ.get("PATH", "")
    bin_str = str(bin_dir)
    if bin_str not in path_value.split(os.pathsep):
        os.environ["PATH"] = f"{bin_str}{os.pathsep}{path_value}" if path_value else bin_str


def _start_background_install() -> None:
    global _INSTALL_THREAD

    with _INSTALL_LOCK:
        if _INSTALL_THREAD and _INSTALL_THREAD.is_alive():
            return

        _INSTALL_THREAD = threading.Thread(target=_install_worker, daemon=True)
        _INSTALL_THREAD.start()


def _install_worker() -> None:
    LOG.info("⬇️ Auto-installing ffmpeg for AutoplayEngineV3 (this may take a few seconds)")
    try:
        _DEPENDENCY_ROOT.mkdir(parents=True, exist_ok=True)
        _download_and_install_ffmpeg()
    except Exception as exc:  # pragma: no cover - network/IO heavy
        LOG.warning("⚠️ Failed to auto-install ffmpeg: %s", exc)


def _download_and_install_ffmpeg() -> None:
    system = platform.system()
    if system == "Windows":
        _install_from_zip(_WINDOWS_RELEASE)
    elif system == "Linux":
        _install_from_tar(_LINUX_RELEASE)
    elif system == "Darwin":
        _install_mac_bundle()
    else:
        raise RuntimeError(f"Unsupported platform for auto ffmpeg install: {system}")


def _install_from_zip(url: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ffmpeg_dl_") as tmp:
        archive_path = Path(tmp) / "ffmpeg.zip"
        _download_file(url, archive_path)
        with zipfile.ZipFile(archive_path) as zf:
            _safe_extract_zip(zf, Path(tmp))
        _copy_binaries(Path(tmp))


def _install_from_tar(url: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ffmpeg_dl_") as tmp:
        archive_path = Path(tmp) / "ffmpeg.tar.xz"
        _download_file(url, archive_path)
        with tarfile.open(archive_path, mode="r:xz") as tf:
            _safe_extract_tar(tf, Path(tmp))
        _copy_binaries(Path(tmp))


def _install_mac_bundle() -> None:
    with tempfile.TemporaryDirectory(prefix="ffmpeg_dl_") as tmp:
        tmp_path = Path(tmp)
        ffmpeg_zip = tmp_path / "ffmpeg_mac.zip"
        ffprobe_zip = tmp_path / "ffprobe_mac.zip"
        _download_file(_MAC_FFMPEG_RELEASE, ffmpeg_zip)
        _download_file(_MAC_FFPROBE_RELEASE, ffprobe_zip)
        with zipfile.ZipFile(ffmpeg_zip) as fzip:
            _safe_extract_zip(fzip, tmp_path / "ffmpeg")
        with zipfile.ZipFile(ffprobe_zip) as pzip:
            _safe_extract_zip(pzip, tmp_path / "ffprobe")
        _copy_binaries(tmp_path)


def _copy_binaries(extracted_root: Path) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    ffmpeg = _find_binary(extracted_root, f"ffmpeg{suffix}")
    ffprobe = _find_binary(extracted_root, f"ffprobe{suffix}")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("Downloaded archive did not contain ffmpeg+ffprobe")

    shutil.rmtree(_FFMPEG_ROOT, ignore_errors=True)
    target_bin = _FFMPEG_BIN
    target_bin.mkdir(parents=True, exist_ok=True)

    shutil.copy2(ffmpeg, target_bin / ffmpeg.name)
    shutil.copy2(ffprobe, target_bin / ffprobe.name)

    for binary in (target_bin / ffmpeg.name, target_bin / ffprobe.name):
        if os.name != "nt":  # Windows handles execute bit separately
            binary.chmod(binary.stat().st_mode | 0o111)

    LOG.info("✅ ffmpeg binaries installed at %s", target_bin)


def _find_binary(root: Path, name: str) -> Optional[Path]:
    for candidate in root.rglob(name):
        if candidate.is_file():
            return candidate
    return None


def _download_file(url: str, destination: Path) -> None:
    LOG.info("⬇️ Downloading %s", url)
    with urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as response, open(destination, "wb") as fh:  # pragma: no cover
        shutil.copyfileobj(response, fh)


def _safe_extract_zip(zf: zipfile.ZipFile, destination: Path) -> None:
    for member in zf.infolist():
        member_path = Path(destination, member.filename).resolve()
        if not str(member_path).startswith(str(destination.resolve())):
            raise RuntimeError("Zip archive attempted path traversal")
    zf.extractall(destination)


def _safe_extract_tar(tf: tarfile.TarFile, destination: Path) -> None:
    dest = destination.resolve()
    for member in tf.getmembers():
        member_path = dest / member.name
        if not str(member_path.resolve()).startswith(str(dest)):
            raise RuntimeError("Tar archive attempted path traversal")
    tf.extractall(destination)


__all__ = [
    "DependencyManager",
    "ensure_ffmpeg_binaries",
    "wait_for_ffmpeg",
    "get_ffmpeg_bin_dir",
    "ensure_model_file",
    "get_model_directory",
    "list_available_models",
    "download_mobilenet_checkpoint",
    "update_embedding_model_env",
    "run_mobilenet_cli",
]


def wait_for_ffmpeg(timeout: float = 15.0, interval: float = 0.5) -> Optional[Path]:
    """Wait up to `timeout` seconds for ffmpeg binaries to become available."""
    start = time.monotonic()
    path = get_ffmpeg_bin_dir()
    if path:
        return path

    ensure_ffmpeg_binaries()
    while time.monotonic() - start < timeout:
        path = get_ffmpeg_bin_dir()
        if path:
            return path
        time.sleep(interval)
    LOG.debug("⚠️ ffmpeg bundle not ready after %.1fs", timeout)
    return None


def get_ffmpeg_bin_dir() -> Optional[Path]:
    """Return the ffmpeg bin directory if already available."""
    return _detect_existing_in_path() or _detect_local_bundle()


def get_model_directory() -> Path:
    """Return the directory where ML checkpoints are stored."""
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    return _MODEL_DIR

def _detect_existing_in_path() -> Optional[Path]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        LOG.debug("✅ System ffmpeg detected at %s", ffmpeg)
        return Path(ffmpeg).resolve().parent
    return None


def _detect_local_bundle() -> Optional[Path]:
    ffmpeg_bin = _FFMPEG_BIN
    suffix = ".exe" if os.name == "nt" else ""
    ffmpeg_path = ffmpeg_bin / f"ffmpeg{suffix}"
    ffprobe_path = ffmpeg_bin / f"ffprobe{suffix}"
    if ffmpeg_path.exists() and ffprobe_path.exists():
        _prepend_path(ffmpeg_bin)
        LOG.info("🎧 Using bundled ffmpeg binaries from %s", ffmpeg_bin)
        return ffmpeg_bin
    return None


def _prepend_path(bin_dir: Path) -> None:
    path_value = os.environ.get("PATH", "")
    bin_str = str(bin_dir)
    if bin_str not in path_value.split(os.pathsep):
        os.environ["PATH"] = f"{bin_str}{os.pathsep}{path_value}" if path_value else bin_str


def _start_background_install() -> None:
    global _INSTALL_THREAD

    with _INSTALL_LOCK:
        if _INSTALL_THREAD and _INSTALL_THREAD.is_alive():
            return

        _INSTALL_THREAD = threading.Thread(target=_install_worker, daemon=True)
        _INSTALL_THREAD.start()


def _install_worker() -> None:
    LOG.info("⬇️ Auto-installing ffmpeg for AutoplayEngineV3 (this may take a few seconds)")
    try:
        _DEPENDENCY_ROOT.mkdir(parents=True, exist_ok=True)
        _download_and_install_ffmpeg()
    except Exception as exc:  # pragma: no cover - network/IO heavy
        LOG.warning("⚠️ Failed to auto-install ffmpeg: %s", exc)


def _download_and_install_ffmpeg() -> None:
    system = platform.system()
    if system == "Windows":
        _install_from_zip(_WINDOWS_RELEASE)
    elif system == "Linux":
        _install_from_tar(_LINUX_RELEASE)
    elif system == "Darwin":
        _install_mac_bundle()
    else:
        raise RuntimeError(f"Unsupported platform for auto ffmpeg install: {system}")


def _install_from_zip(url: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ffmpeg_dl_") as tmp:
        archive_path = Path(tmp) / "ffmpeg.zip"
        _download_file(url, archive_path)
        with zipfile.ZipFile(archive_path) as zf:
            _safe_extract_zip(zf, Path(tmp))
        _copy_binaries(Path(tmp))


def _install_from_tar(url: str) -> None:
    with tempfile.TemporaryDirectory(prefix="ffmpeg_dl_") as tmp:
        archive_path = Path(tmp) / "ffmpeg.tar.xz"
        _download_file(url, archive_path)
        with tarfile.open(archive_path, mode="r:xz") as tf:
            _safe_extract_tar(tf, Path(tmp))
        _copy_binaries(Path(tmp))


def _install_mac_bundle() -> None:
    with tempfile.TemporaryDirectory(prefix="ffmpeg_dl_") as tmp:
        tmp_path = Path(tmp)
        ffmpeg_zip = tmp_path / "ffmpeg_mac.zip"
        ffprobe_zip = tmp_path / "ffprobe_mac.zip"
        _download_file(_MAC_FFMPEG_RELEASE, ffmpeg_zip)
        _download_file(_MAC_FFPROBE_RELEASE, ffprobe_zip)
        with zipfile.ZipFile(ffmpeg_zip) as fzip:
            _safe_extract_zip(fzip, tmp_path / "ffmpeg")
        with zipfile.ZipFile(ffprobe_zip) as pzip:
            _safe_extract_zip(pzip, tmp_path / "ffprobe")
        _copy_binaries(tmp_path)


def _copy_binaries(extracted_root: Path) -> None:
    suffix = ".exe" if os.name == "nt" else ""
    ffmpeg = _find_binary(extracted_root, f"ffmpeg{suffix}")
    ffprobe = _find_binary(extracted_root, f"ffprobe{suffix}")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("Downloaded archive did not contain ffmpeg+ffprobe")

    shutil.rmtree(_FFMPEG_ROOT, ignore_errors=True)
    target_bin = _FFMPEG_BIN
    target_bin.mkdir(parents=True, exist_ok=True)

    shutil.copy2(ffmpeg, target_bin / ffmpeg.name)
    shutil.copy2(ffprobe, target_bin / ffprobe.name)

    for binary in (target_bin / ffmpeg.name, target_bin / ffprobe.name):
        if os.name != "nt":  # Windows handles execute bit separately
            binary.chmod(binary.stat().st_mode | 0o111)

    LOG.info("✅ ffmpeg binaries installed at %s", target_bin)


def _find_binary(root: Path, name: str) -> Optional[Path]:
    for candidate in root.rglob(name):
        if candidate.is_file():
            return candidate
    return None


def _download_file(url: str, destination: Path) -> None:
    LOG.info("⬇️ Downloading %s", url)
    with urlopen(url, timeout=_DOWNLOAD_TIMEOUT) as response, open(destination, "wb") as fh:  # pragma: no cover
        shutil.copyfileobj(response, fh)


def _safe_extract_zip(zf: zipfile.ZipFile, destination: Path) -> None:
    for member in zf.infolist():
        member_path = Path(destination, member.filename).resolve()
        if not str(member_path).startswith(str(destination.resolve())):
            raise RuntimeError("Zip archive attempted path traversal")
    zf.extractall(destination)


def _safe_extract_tar(tf: tarfile.TarFile, destination: Path) -> None:
    dest = destination.resolve()
    for member in tf.getmembers():
        member_path = dest / member.name
        if not str(member_path.resolve()).startswith(str(dest)):
            raise RuntimeError("Tar archive attempted path traversal")
    tf.extractall(destination)


def _parse_mobilenet_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download EfficientAT MobileNet checkpoints for Autoplay V3",
    )
    parser.add_argument(
        "--model",
        choices=list(_MOBILENET_MODELS.keys()),
        default="mn10_as",
        help="Checkpoint to download",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the file already exists",
    )
    parser.add_argument(
        "--set-env",
        action="store_true",
        help="Update .env with the selected embedding model",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available models and exit",
    )
    return parser.parse_args(argv)


def run_mobilenet_cli(argv: Sequence[str] | None = None) -> int:
    args = _parse_mobilenet_args(argv)

    if args.list:
        print("Available checkpoints:")
        for key, info in list_available_models().items():
            label = info.get("label", info["filename"])
            print(f"  {key:8s} -> {label} ({info['filename']})")
        return 0

    model_key = args.model
    info = _MOBILENET_MODELS[model_key]
    print(f"🚀 Preparing to download {info.get('label', info['filename'])}...")
    destination = download_mobilenet_checkpoint(model_key, force=args.force)
    if args.set_env:
        updated = update_embedding_model_env(info.get("embedding_model", model_key))
        if not updated:
            print("⚠️ .env file not updated (see logs for details)")
    print("All done! Restart the bot to pick up the new checkpoint.")
    print(f"Checkpoint stored at: {destination}")
    return 0


if __name__ == "__main__":  # pragma: no cover - manual helper
    raise SystemExit(run_mobilenet_cli(sys.argv[1:]))
