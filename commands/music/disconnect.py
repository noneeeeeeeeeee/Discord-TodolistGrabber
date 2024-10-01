import discord
from discord.ext import commands
import asyncio
from .disconnect_state import DisconnectState 

class Disconnect(commands.Cog):
    def __init__(self, bot, disconnect_state: DisconnectState):
        self.bot = bot
        self.disconnect_task = {}
        self.disconnect_state = disconnect_state 

    async def schedule_disconnect(self, ctx):
        """Schedules a disconnect after 5 minutes of inactivity."""
        await asyncio.sleep(300)
        if ctx.guild.voice_client and not ctx.guild.voice_client.is_playing():
            self.disconnect_state.set_intentional() 
            await ctx.guild.voice_client.disconnect()
            await ctx.send(":wave: Disconnected due to inactivity.")

    @commands.command(name="disconnect", aliases=["dc"])
    async def disconnect(self, ctx):
        """Immediately disconnects the bot from the voice channel."""
        if ctx.guild.voice_client and ctx.guild.voice_client.is_connected():
            self.disconnect_state.set_intentional()  
            await ctx.guild.voice_client.disconnect()
            await ctx.send(":wave: Disconnected from the voice channel.")
            # Cancel any active disconnect task
            if self.disconnect_task.get(ctx.guild.id):
                self.disconnect_task[ctx.guild.id].cancel()
                del self.disconnect_task[ctx.guild.id]
        else:
            await ctx.send(":x: I am not connected to any voice channel.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if after.channel is None and member.id == self.bot.user.id:
            return

        if after.channel is None:
            if member.guild.voice_client and member.guild.voice_client.is_connected() and \
                    len(member.guild.voice_client.channel.members) == 1:
                await asyncio.sleep(180)  # Wait for 3 minutes
            if member.guild.voice_client and member.guild.voice_client.is_connected() and \
                    len(member.guild.voice_client.channel.members) == 1:
                self.disconnect_state.set_intentional()  # Set intentional disconnect
                await member.guild.voice_client.disconnect()
                channel = member.guild.system_channel
                if channel:
                    await channel.send(":wave: The bot has disconnected since the channel is empty.")
    
    async def cog_command_error(self, ctx, error):
        """Handles errors that occur within the cog's commands."""
        await ctx.send(f"An error occurred: {str(error)}")

async def setup(bot):
    disconnect_state = DisconnectState()  
    await bot.add_cog(Disconnect(bot, disconnect_state)) 
