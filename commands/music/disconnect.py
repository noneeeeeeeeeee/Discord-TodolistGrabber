import discord
from discord.ext import commands
import asyncio
from modules.disconnect_state import DisconnectState
from .clearqueue import ClearQueue


class Disconnect(commands.Cog):
    def __init__(self, bot, disconnect_state: DisconnectState):
        self.bot = bot
        self.disconnect_task = {}
        self.disconnect_state = disconnect_state

    async def schedule_disconnect(self, ctx):
        """Schedules a disconnect after 5 minutes of inactivity."""
        if self.disconnect_task.get(ctx.guild.id):
            self.disconnect_task[ctx.guild.id].cancel()
        self.disconnect_task[ctx.guild.id] = self.bot.loop.create_task(
            self._disconnect_after_inactivity(ctx)
        )

    async def _disconnect_after_inactivity(self, ctx):
        await asyncio.sleep(300)  # 5 minutes
        music_player = self.bot.get_cog("MusicPlayer")
        if (
            ctx.guild.voice_client
            and not ctx.guild.voice_client.is_playing()
            and not music_player.now_playing.get(ctx.guild.id)
        ):
            self.disconnect_state.set_intentional()
            await ctx.guild.voice_client.disconnect()
            await ctx.send(":wave: Disconnected due to inactivity.")
            await self.clear_queue(ctx)
            # Remove the task from the dictionary
            del self.disconnect_task[ctx.guild.id]

    @commands.hybrid_command(
        name="disconnect",
        aliases=["dc"],
        description="Immediately disconnects the bot from the voice channel.",
    )
    async def disconnect(self, ctx):
        """Immediately disconnects the bot from the voice channel."""
        if ctx.guild.voice_client and ctx.guild.voice_client.is_connected():
            self.disconnect_state.set_intentional()
            await ctx.guild.voice_client.disconnect()
            await ctx.send(":wave: Disconnected from the voice channel.")
            await self.clear_queue(ctx)
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
            if (
                member.guild.voice_client
                and member.guild.voice_client.is_connected()
                and len(member.guild.voice_client.channel.members) == 1
            ):
                await asyncio.sleep(3)
            if (
                member.guild.voice_client
                and member.guild.voice_client.is_connected()
                and len(member.guild.voice_client.channel.members) == 1
            ):
                self.disconnect_state.set_intentional()
                await member.guild.voice_client.disconnect()
                channel = member.guild.system_channel
                if channel:
                    await channel.send(
                        ":wave: The bot has disconnected since the channel is empty."
                    )
                await self.clear_queue(member.guild)

    async def clear_queue(self, ctx):
        """Clears the music queue and stops all music-related tasks."""
        music_player = self.bot.get_cog("MusicPlayer")
        if music_player:
            guild_id = ctx.guild.id
            if guild_id in music_player.music_queue:
                music_player.music_queue[guild_id].clear()
            if guild_id in music_player.now_playing:
                del music_player.now_playing[guild_id]
            if ctx.guild.voice_client and ctx.guild.voice_client.is_playing():
                ctx.guild.voice_client.stop()

        clear_queue_cog = self.bot.get_cog("ClearQueue")
        if clear_queue_cog:
            await clear_queue_cog.clear_queue(ctx)

    async def cog_command_error(self, ctx, error):
        """Handles errors that occur within the cog's commands."""
        await ctx.send(f"An error occurred: {str(error)}")


async def setup(bot):
    disconnect_state = DisconnectState()
    await bot.add_cog(Disconnect(bot, disconnect_state))
