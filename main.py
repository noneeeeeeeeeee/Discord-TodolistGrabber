import discord
from discord.ext import commands
import os
from modules.enviromentfilegenerator import check_and_load_env_file

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

# Initialize slash commands
class MyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def setup_hook(self):
        # Sync the slash commands with Discord
        await load_commands()
        await self.tree.sync()

async def load_commands():
    # Load commands from the commands directory
    commands_dir = os.path.join(os.path.dirname(__file__), 'commands')
    if not os.path.exists(commands_dir):
        print(f"Commands directory '{commands_dir}' does not exist.")
        return

    for filename in os.listdir(commands_dir):
        if filename.endswith('.py'):
            try:
                await bot.load_extension(f'commands.{filename[:-3]}')
            except Exception as e:
                print(f"Failed to load extension {filename}: {e}")

    # Load the Status cog
    try:
        await bot.load_extension('commands.status')
    except Exception as e:
        print(f"Failed to load Status cog: {e}")

bot = MyBot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')

# Run the bot with your token
check_and_load_env_file()
bot.run(os.getenv('DiscordBotToken'))
