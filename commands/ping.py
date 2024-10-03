import discord
from discord.ext import commands
from discord import app_commands
import time

class Ping(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name='ping', description='Check the bot\'s latency.')
    @commands.cooldown(1, 60, commands.BucketType.user) 
    async def ping(self, ctx: commands.Context):
        start_time = time.time()
        message = await ctx.reply("Pinging...")
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

    # Error handler to catch the cooldown error
    @ping.error
    async def ping_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            retry_after = round(error.retry_after, 1)
            await ctx.send(f":hourglass: Please wait {retry_after} seconds before using the `ping` command again.", time=5)
        else:
            await ctx.send(":x: An unexpected error occurred.")

async def setup(bot):
    await bot.add_cog(Ping(bot))
