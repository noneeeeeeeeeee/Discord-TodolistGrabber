import discord
from discord.ext import commands
import asyncio

class Disconnect(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.disconnect_task = {}

    async def schedule_disconnect(self, ctx):
        await asyncio.sleep(300)  
        if ctx.guild.voice_client and not ctx.guild.voice_client.is_playing():
            await ctx.guild.voice_client.disconnect()
            await ctx.send("Disconnected due to inactivity.")

    @commands.command(name="disconnect", aliases=["dc"])
    async def disconnect(self, ctx):
        if ctx.guild.voice_client and ctx.guild.voice_client.is_connected():
            await ctx.guild.voice_client.disconnect()
            await ctx.send("Disconnected from the voice channel.")
            # Cancel any active disconnect task
            if self.disconnect_task.get(ctx.guild.id):
                self.disconnect_task[ctx.guild.id].cancel()
                del self.disconnect_task[ctx.guild.id]
        else:
            await ctx.send("I am not connected to any voice channel.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if after.channel is None:  # User left the voice channel
            if member.guild.voice_client and member.guild.voice_client.is_connected():
                await member.guild.voice_client.disconnect()
                await member.send("The bot has disconnected since the channel is empty.")

    async def cog_command_error(self, ctx, error):
        await ctx.send(f"An error occurred: {str(error)}")

async def setup(bot):
    await bot.add_cog(Disconnect(bot))
