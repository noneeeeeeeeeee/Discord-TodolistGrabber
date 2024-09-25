from dotenv import load_dotenv
import os

def check_and_load_env_file():
    # Load .env file
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    if not os.path.exists(env_path):
        example_env_content = (
            "API_URL=https://example.com/api\n"
            "DiscordBotToken=YOUR_DISCORD_BOT_TOKEN\n"
            "GeminiApiKey=YOUR_GEMINI_API_KEY\n"
            "FFMPEG_PATH=C:/ffmpeg/bin/ffmpeg.exe\n"
            "YOUTUBE_API_KEY=YOUR_YOUTUBE_API_KEY\n"
        )
        with open(env_path, 'w') as env_file:
            env_file.write(example_env_content)
            return "No .env file found. Creating one with example content."
    return load_dotenv(env_path)
            