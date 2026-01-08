import discord
from discord.ext import commands
import os
import json
import sys
from modules.enviromentfilegenerator import check_and_load_env_file
import subprocess


def _run_first_start_dependencies() -> None:
    """Download required dependencies on first/fresh start with progress bar."""
    try:
        from modules.music.Autoplay_Engine.v3.dependency_manager import (
            DependencyManager,
            get_model_directory,
            _MOBILENET_MODELS,
        )
        
        model_dir = get_model_directory()
        model_key = "mn10_as"
        model_info = _MOBILENET_MODELS.get(model_key, {})
        model_filename = model_info.get("filename", "mn10_as_mAP_471.pt")
        model_path = model_dir / model_filename
        
        # Check if this is a fresh start (model doesn't exist)
        if not model_path.exists():
            print("\n" + "=" * 60)
            print("🚀 First Start Detected - Downloading Dependencies")
            print("=" * 60)
            
            # Download model with progress bar
            from modules.music.Autoplay_Engine.v3.dependency_manager import (
                download_mobilenet_checkpoint,
            )
            print(f"\n📦 Downloading EfficientAT model ({model_filename})...")
            download_mobilenet_checkpoint(model_key, force=False, show_progress=True)
            print(f"✅ Model saved to: {model_path}")
            
            # Also ensure ffmpeg is available
            dm = DependencyManager(model_key=model_key)
            print("\n🔧 Checking ffmpeg availability...")
            ffmpeg_path = dm.ensure_ffmpeg(timeout=60.0)
            if ffmpeg_path:
                print(f"✅ ffmpeg ready at: {ffmpeg_path}")
            else:
                print("⚠️ ffmpeg not found - will attempt background download")
            
            print("\n" + "=" * 60)
            print("✅ Dependencies ready! Starting bot...")
            print("=" * 60 + "\n")
        else:
            print(f"✅ Dependencies already present ({model_filename})")
            
    except ImportError as e:
        print(f"⚠️ V3 dependency manager not available: {e}")
    except Exception as e:
        print(f"⚠️ Dependency check failed (non-fatal): {e}")


intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

CONFIG_DIR = "./config"


class MyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def setup_hook(self):
        """Load cogs and sync commands."""
        await load_commands()
        try:
            await self.load_extension("modules.music.music_player")
            print("MusicPlayer extension loaded")
        except Exception as e:
            print(f"Failed to load MusicPlayer cog: {e}")
        await self.tree.sync()

    async def on_socket_raw_receive(self, message):
        """Re-dispatch raw gateway payloads for libraries expecting socket_response."""
        await super().on_socket_raw_receive(message)

        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError:
                return

        if not isinstance(message, str):
            return

        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            return

        if isinstance(payload, dict):
            self.dispatch("socket_response", payload)


async def load_commands():
    """Load all cogs from the commands directory."""
    commands_dir = os.path.join(os.path.dirname(__file__), "commands")
    if not os.path.exists(commands_dir):
        print(f"Commands directory '{commands_dir}' does not exist.")
        return

    for root, dirs, files in os.walk(commands_dir):
        for filename in files:
            if filename.endswith(".py"):
                module_path = os.path.relpath(
                    os.path.join(root, filename), start=commands_dir
                )
                cog_name = f'commands.{module_path[:-3].replace(os.path.sep, ".")}'  # Replace path separators with dots

                if cog_name in bot.extensions:
                    print(f"Unloading previously loaded cog: {cog_name}")
                    await bot.unload_extension(cog_name)

                try:
                    await bot.load_extension(cog_name)
                    print(f"Successfully loaded extension {filename}")
                except Exception as e:
                    print(f"Failed to load extension {filename}: {e}")


bot = MyBot(command_prefix="!", intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")


check_and_load_env_file()

# Download V3 dependencies on first start (before bot runs)
_run_first_start_dependencies()

bot_token = os.getenv("DiscordBotToken")

if not bot_token:
    print("Error: Discord bot token is missing. Please set it in the .env file.")
else:
    try:
        bot.run(bot_token)
    except:
        print("The bot token is invalid. Please check the token in the .env file.")
