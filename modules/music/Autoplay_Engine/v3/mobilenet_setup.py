"""Helper script for downloading EfficientAT MobileNet checkpoints and updating .env."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict

import requests

from .dependency_manager import get_model_directory

MOBILENET_MODELS: Dict[str, Dict[str, str]] = {
    "mn10_as": {
        "label": "EfficientAT MobileNetV3 Large (mn10_as, 527 logits)",
        "filename": "mn10_as_mAP_471.pt",
        "url": "https://github.com/fschmid56/EfficientAT/releases/download/v0.0.1/mn10_as_mAP_471.pt",
        "embedding_model": "mn10_as",
    }
}

BUFFER_SIZE = 1024 * 1024  # 1 MiB


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _env_path() -> Path:
    return _repo_root() / ".env"


def download_checkpoint(model_key: str, *, force: bool = False) -> Path:
    info = MOBILENET_MODELS[model_key]
    model_dir = get_model_directory()
    destination = model_dir / info["filename"]
    if destination.exists() and not force:
        print(f"✅ {destination.name} already exists. Use --force to re-download.")
        return destination

    response = requests.get(info["url"], stream=True, timeout=60)
    response.raise_for_status()
    total = int(response.headers.get("content-length", "0"))
    downloaded = 0
    tmp_path = destination.with_suffix(destination.suffix + ".partial")
    with open(tmp_path, "wb") as fh:
        for chunk in response.iter_content(chunk_size=BUFFER_SIZE):
            if not chunk:
                continue
            fh.write(chunk)
            downloaded += len(chunk)
            if total:
                percent = downloaded / total * 100
                print(f"\r⬇️  Downloading {destination.name}: {percent:5.1f}%", end="", flush=True)
    if total:
        print()
    tmp_path.replace(destination)
    print(f"✅ Saved checkpoint to {destination}")
    return destination


def update_env_file(embedding_model: str) -> None:
    env_path = _env_path()
    if not env_path.exists():
        print("⚠️ .env file not found. Run the bot once or generate it before using --set-env.")
        return

    lines = env_path.read_text().splitlines()
    target_line = f"EMBEDDING_MODEL={embedding_model}"
    for idx, line in enumerate(lines):
        if line.strip().startswith("EMBEDDING_MODEL="):
            lines[idx] = target_line
            break
    else:
        lines.append(target_line)
    env_path.write_text("\n".join(lines) + "\n")
    print(f"📝 Updated {env_path.name} -> {target_line}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download EfficientAT MobileNet checkpoints for Autoplay V2")
    parser.add_argument("--model", choices=MOBILENET_MODELS.keys(), help="Checkpoint to download", default="mn10_as")
    parser.add_argument("--force", action="store_true", help="Re-download even if the file already exists")
    parser.add_argument("--set-env", action="store_true", help="Update .env to use EMBEDDING_MODEL")
    parser.add_argument("--list", action="store_true", help="List available models and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.list:
        print("Available checkpoints:")
        for key, info in MOBILENET_MODELS.items():
            print(f"  {key:8s} -> {info['label']} ({info['filename']})")
        return 0

    model_key = args.model
    info = MOBILENET_MODELS[model_key]
    print(f"🚀 Preparing to download {info['label']}...")
    destination = download_checkpoint(model_key, force=args.force)
    if args.set_env:
        update_env_file(info["embedding_model"])
    print("All done! Restart the bot to pick up the new checkpoint.")
    print(f"Checkpoint stored at: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
