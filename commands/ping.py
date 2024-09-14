import discord
from discord.ext import commands
import time

class Ping(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def ping(self, ctx):
        start_time = time.time()
        message = await ctx.send("Pinging...")
        end_time = time.time()
        
        latency = (end_time - start_time) * 1000  # Convert to milliseconds
        latency = int(latency)
        
        if latency < 500:
            color = discord.Color.green()
        elif 500 <= latency < 1000:
            color = discord.Color.gold()
        else:
            color = discord.Color.red()
        
        embed = discord.Embed(
            title="Pong! :ping_pong:",
            description=f"Response time: {latency}ms",
            color=color
        )
        
        await message.edit(content=None, embed=embed)

async def setup(bot):
    await bot.add_cog(Ping(bot))