import discord
from discord.ext import commands
import os
from modules.enviromentfilegenerator import check_and_load_env_file

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Load commands from the commands directory
for filename in os.listdir('./commands'):
    if filename.endswith('.py'):
        bot.load_extension(f'commands.{filename[:-3]}')

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')

# Run the bot with your token
check_and_load_env_file()
bot.run(os.getenv('DiscordBotToken'))