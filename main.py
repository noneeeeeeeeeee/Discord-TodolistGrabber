import discord
from discord.ext import commands
import os
import json
from modules.enviromentfilegenerator import check_and_load_env_file

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

CONFIG_DIR = "./config"  # Directory where guild setup status is stored

class MyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def setup_hook(self):
        """Load cogs and sync commands."""
        await load_commands()
        await self.tree.sync()

async def load_commands():
    """Load all cogs from the commands directory."""
    commands_dir = os.path.join(os.path.dirname(__file__), 'commands')
    if not os.path.exists(commands_dir):
        print(f"Commands directory '{commands_dir}' does not exist.")
        return

    for root, dirs, files in os.walk(commands_dir):
        for filename in files:
            if filename.endswith('.py'):
                # Construct the module name based on the folder structure
                module_path = os.path.relpath(os.path.join(root, filename), start=commands_dir)
                cog_name = f'commands.{module_path[:-3].replace(os.path.sep, ".")}'  # Replace path separators with dots

                if cog_name in bot.extensions:
                    print(f"Unloading previously loaded cog: {cog_name}")
                    await bot.unload_extension(cog_name)

                try:
                    await bot.load_extension(cog_name)
                    print(f"Successfully loaded extension {filename}")
                except Exception as e:
                    print(f"Failed to load extension {filename}: {e}")
    
bot = MyBot(command_prefix='!', intents=intents, help_command=None)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')

print(check_and_load_env_file())

bot_token = os.getenv('DiscordBotToken')

if not bot_token:
    print("Error: Discord bot token is missing. Please set it in the .env file.")
else:
    try:
        bot.run(bot_token)
    except:
        print("The bot token is invalid. Please check the token in the .env file.")