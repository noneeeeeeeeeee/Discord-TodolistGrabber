# Welcome to the Discord-TodolistGrabber

## Setting up music (Lavalink)

- This bot uses Lavalink for audio playback via the wavelink client.
- Requirements:
  - Java 17+ installed.
  - A running Lavalink server. See https://github.com/lavalink-devs/Lavalink

Manual setup (local node)

1. Download Lavalink.jar from https://github.com/lavalink-devs/Lavalink/releases/latest
2. Place these files in:
   d:\dev\Github\Discord-TodolistGrabber\modules\music\lavalink
   - Lavalink.jar
   - application.yml (your Lavalink config)
3. Minimal application.yml example:
   server:
   port: 2333
   address: 0.0.0.0
   lavalink:
   server:
   password: "youshallnotpass"
   sources:
   youtube: true
   bandcamp: true
   soundcloud: true
   twitch: true
   vimeo: true
   http: true
   local: false
4. Bot startup will auto-run Lavalink if the files exist.
   - Optional manual run: java -jar Lavalink.jar
5. Ensure your .env matches the node:
   LAVALINK_HOST=127.0.0.1
   LAVALINK_PORT=2333
   LAVALINK_PASSWORD=youshallnotpass
   LAVALINK_SECURE=false
   LAVALINK_JAVA_PATH=java (optional override)
   LAVALINK_AUTO_START=true (default in this repo)

Notes

- The bot auto-reconnects to Lavalink and will auto-disconnect from voice after inactivity.
- If the bot cannot connect to Lavalink, it prints setup instructions in the console/logs.
- You can limit concurrent music instances globally via Settings > Music > MaxConcurrentInstances (owner only).
