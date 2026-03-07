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
            "# Provide one or more API keys as a JSON array (first entry is primary).\n"
            'GeminiApiKeys=["YOUR_PRIMARY_GEMINI_API_KEY", "YOUR_SECONDARY_GEMINI_API_KEY"]\n'
            "\n"
            "# Lavalink Audio Server Settings (For Advanced Users, Else Keep default)\n"
            "LAVALINK_HOST=127.0.0.1\n"
            "LAVALINK_PORT=2333\n"
            "LAVALINK_PASSWORD=youshallnotpass\n"
            "LAVALINK_SECURE=false\n"
            "\n"
            "LASTFM_API_KEY=YOUR_LASTFM_API_KEY\n"
            "# V3 Audio Analysis Configuration\n"
            "# ANALYSIS_MODE: 'ml' (high accuracy, requires models) or 'non-ml' (lightweight, Librosa-only)\n"
            "ANALYSIS_MODE=ml\n"
        )
        with open(env_path, "w") as env_file:
            env_file.write(example_env_content)
            return "No .env file found. A .env file has been generated for you with placeholder content. Please fill in all of it."
    return load_dotenv(env_path)
