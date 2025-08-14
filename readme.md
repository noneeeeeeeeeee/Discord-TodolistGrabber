# Welcome to the Discord-TodolistGrabber

## Setting up music (Lavalink)

- This bot uses Lavalink for audio playback via the wavelink client.
- Requirements:
  - A running Lavalink server (Java 17+). See https://github.com/lavalink-devs/Lavalink
  - Configure the connection in your .env:
    - LAVALINK_HOST=127.0.0.1
    - LAVALINK_PORT=2333
    - LAVALINK_PASSWORD=youshallnotpass
    - LAVALINK_SECURE=false
- Windows users: You no longer need to install FFmpeg for basic playback when using Lavalink. Lavalink handles streaming internally.

Notes

- The bot auto-reconnects to Lavalink and will auto-disconnect from voice after inactivity.
- You can limit concurrent music instances globally via Settings > Music > MaxConcurrentInstances (owner only).
