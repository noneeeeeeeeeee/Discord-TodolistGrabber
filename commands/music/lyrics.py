import discord
from discord.ext import commands
import os
import aiohttp
import urllib.parse
import json
from modules.readversion import read_current_version


class Lyrics(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.current_lyrics_message = None
        self.current_song = None

    @commands.command(name="ly", aliases=["lyrics"])
    async def display_lyrics(self, ctx):
        await ctx.send("Will be implemented in the future!")


async def setup(bot):
    await bot.add_cog(Lyrics(bot))
