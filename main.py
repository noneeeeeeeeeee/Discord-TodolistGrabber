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
        # Sync the slash commands with Discord
        await load_commands()
        await self.tree.sync()

    def is_guild_setup(self, guild_id):
        """Check if the guild is already set up by checking for a config file."""
        guild_config_path = os.path.join(CONFIG_DIR, f"{guild_id}.json")
        return os.path.exists(guild_config_path)

    async def on_message(self, message):
        """Override on_message to check if commands are enabled for the guild."""
        if message.author == self.user:
            return  # Ignore messages from the bot itself

        guild_id = message.guild.id
        config_path = os.path.join(CONFIG_DIR, f"{guild_id}.json")

        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                status = json.load(f)
                if not status.get("commands_enabled", True):
                    if message.content.startswith(self.command_prefix):
                        embed = discord.Embed(
                            title="Setup Required",
                            description="The bot is not set up yet. Please run `!setup` to configure it.",
                            color=discord.Color.red()
                        )
                        await message.channel.send(embed=embed)
                        return

        await self.process_commands(message)

    async def enable_commands_for_guild(self, guild):
        """Set a flag to enable commands for the guild after setup is complete."""
        config_path = os.path.join(CONFIG_DIR, f"{guild.id}.json")
        with open(config_path, 'w') as f:
            json.dump({"commands_enabled": True}, f)
        print(f"Commands enabled for guild {guild.id}")

async def load_commands():
    """Load all cogs from the commands directory."""
    commands_dir = os.path.join(os.path.dirname(__file__), 'commands')
    if not os.path.exists(commands_dir):
        print(f"Commands directory '{commands_dir}' does not exist.")
        return

    for filename in os.listdir(commands_dir):
        if filename.endswith('.py'):
            cog_name = f'commands.{filename[:-3]}'

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

# Load environment variables from the .env file
check_and_load_env_file()

# Retrieve the bot token from the .env file
bot_token = os.getenv('DiscordBotToken')

if not bot_token:
    print("Error: Discord bot token is missing. Please set it in the .env file.")
else:
    # Run the bot
    bot.run(bot_token)
