# Discord-TodolistGrabber

Modern Discord bot for homework tracking and utility modules with an advanced music subsystem powered by Lavalink.

## Getting Started

1. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

2. Run the bot once so it can generate `.env` and default config files.
3. Populate `.env` with your Discord token, JSON-formatted `GeminiApiKeys` (first key is primary, add more for rotation), and (optionally) Lavalink host credentials. If you keep the defaults, the bot can auto-provision Lavalink locally.
4. (Optional, but recommended) Download the EfficientAT MobileNet checkpoint for the V3 audio analysis pipeline:

   ```powershell
   # new consolidated helper (preferred)
   python -m modules.music.Autoplay_Engine.v3.dependency_manager --model mn10_as --set-env

   # backward-compatible shim (still works and simply forwards to the helper above)
   python -m modules.music.Autoplay_Engine.v3.mobilenet_setup --model mn10_as --set-env
   ```

   The helper stores `mn10_as_mAP_471.pt` in `modules/music/Autoplay_Engine/v3/dependencies/models` and updates `.env` so Autoplay knows which embedding size to expect. Legacy PANN model names are still understood but automatically mapped to the EfficientAT `mn10_as` pipeline—no separate PANN inference remains.
5. Invite the bot to your server and start it.

---

## Bot Technical Details

## Music - Autoplay Engine

### Overview

Autoplay V3 is now the default engine. It combines Deezer-native ingest, Gemini enrichment, and an EfficientAT MobileNet analyzer to produce 5D vibe vectors plus high-dimensional embeddings. The legacy V1 (Last.fm-driven) engine remains available as a fallback via `modules/music/Autoplay_Engine/config.py` if you need a minimal, feedback-less experience.
