from dotenv import load_dotenv
import os


def check_and_load_env_file():
    # Load .env file
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(env_path):
        example_env_content = (
            "# API Endpoints\n"
            "API_URL=https://example.com/api\n"
            "AUTH_URL=YOUR_AUTH_URL_HERE\n"
            "\n"
            "# Discord Bot Configuration\n"
            "DiscordBotToken=YOUR_DISCORD_BOT_TOKEN\n"
            "OWNER_ID=YOUR_DISCORD_ID\n"
            "MAIN_GUILD=YOUR_MAIN_GUILD_ID\n"
            "LOCAL_REGION=YOUR_LOCAL_REGION\n"
            "\n"
            "# Gemini / AI Keys\n"
            "GeminiApiKey=YOUR_GEMINI_API_KEY\n"
            "\n"
            "# Lavalink Audio Server Settings\n"
            "LAVALINK_HOST=127.0.0.1\n"
            "LAVALINK_PORT=2333\n"
            "LAVALINK_PASSWORD=youshallnotpass\n"
            "LAVALINK_SECURE=false\n"
            "\n"
            "# Spotify API (Optional - for intelligent autoplay recommendations)\n"
            "# Get credentials from: https://developer.spotify.com/dashboard\n"
            "SPOTIFY_CLIENT_ID=YOUR_SPOTIFY_CLIENT_ID\n"
            "SPOTIFY_CLIENT_SECRET=YOUR_SPOTIFY_CLIENT_SECRET\n"
        )
        with open(env_path, "w") as env_file:
            env_file.write(example_env_content)
            return "No .env file found. A .env file has been generated for you with placeholder content. Please fill in all of it."
    return load_dotenv(env_path)
