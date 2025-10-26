# Discord-TodolistGrabber

Modern Discord bot for homework tracking and utility modules with an advanced music subsystem powered by Lavalink.

## Getting Started

1. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
2. Run the bot once so it can generate `.env` and default config files.
3. Populate `.env` with your Discord token, JSON-formatted `GeminiApiKeys` (first key is primary, add more for rotation), and (optionally) Lavalink host credentials. If you keep the defaults, the bot can auto-provision Lavalink locally.
4. Invite the bot to your server and start it.

---

# Bot Technical Details

## Music - Autoplay Engine

### Overview

This is now outdated. v2 of the algorithm engine is out this lays the foundation for v3!
v3 will introduce the collaborative matrix, machine learning prediction, and heuristics improvements!
