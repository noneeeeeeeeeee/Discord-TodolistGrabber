from dotenv import load_dotenv
import os


def check_and_load_env_file():
    # Load .env file
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if not os.path.exists(env_path):
        example_env_content = (
            "API_URL=https://example.com/api\n"
            "DiscordBotToken=YOUR_DISCORD_BOT_TOKEN\n"
            "GeminiApiKey=YOUR_GEMINI_API_KEY\n"
            "OWNER_ID=YOUR_DISCORD_ID\n"
            "MAIN_GUILD=YOUR_MAIN_GUILD_ID\n"
            "LAVALINK_HOST=127.0.0.1\n"
            "LAVALINK_PORT=2333\n"
            "LAVALINK_PASSWORD=youshallnotpass\n"
            "LAVALINK_SECURE=false\n"
        )
        with open(env_path, "w") as env_file:
            env_file.write(example_env_content)
            return "No .env file found. A .env file has been generated for you with placeholder content. Please fill in all of it."
    return load_dotenv(env_path)
