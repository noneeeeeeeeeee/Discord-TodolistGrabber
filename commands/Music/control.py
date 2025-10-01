import discord
from discord.ext import commands

from modules.music.music_player import (
    is_voice_connected,
    is_voice_paused,
    is_voice_playing,
)


class ControlCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="pause", description="Pause playback.")
    async def pause(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc and is_voice_playing(vc):
            vc.pause()
            await ctx.send("Paused playback.")
        else:
            await ctx.send(":x: Nothing is playing.")

    @commands.hybrid_command(name="resume", description="Resume playback.")
    async def resume(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc and is_voice_paused(vc):
            vc.resume()
            await ctx.send("Resumed playback.")
        else:
            await ctx.send(":x: Nothing to resume.")

    @commands.hybrid_command(
        name="stop", description="Stop and clear queue (DJ/Admin)."
    )
    async def stop(self, ctx: commands.Context):
        # permission check
        if not (
            ctx.author.guild_permissions.administrator
            or ctx.author.guild_permissions.manage_guild
        ):
            await ctx.send(":x: Admin/DJ required.")
            return
        vc = ctx.guild.voice_client
        if vc:
            vc.stop()
        player = self.bot.get_cog("MusicPlayer")
        player.queues[ctx.guild.id].clear()
        await ctx.send("Stopped and cleared queue.")

    @commands.hybrid_command(
        name="disconnect", description="Disconnect the bot from the voice channel."
    )
    async def disconnect(self, ctx: commands.Context):
        if not (
            ctx.author.guild_permissions.administrator
            or ctx.author.guild_permissions.manage_guild
        ):
            await ctx.send(":x: Admin/DJ required.")
            return

        vc = ctx.guild.voice_client
        if not vc or not is_voice_connected(vc):
            await ctx.send("Bot is not connected to a voice channel.")
            return

        try:
            await vc.disconnect()
        except Exception as exc:
            await ctx.send(f":x: Failed to disconnect: {exc}")
            return

        player = self.bot.get_cog("MusicPlayer")
        if player:
            await player._cancel_idle(ctx.guild.id)
            if await player.send_disconnect_message(
                ctx.guild, "bot_disconnected_by_user", channel=ctx.channel
            ):
                return
        await ctx.send(":wave: Disconnected from voice channel.")

    @commands.hybrid_command(name="volume", description="Set default volume (0-200).")
    async def volume(self, ctx: commands.Context, value: int):
        if value < 0 or value > 200:
            await ctx.send(":x: Volume must be 0-200.")
            return
        try:
            from modules.setconfig import edit_json_file

            edit_json_file(
                ctx.guild.id,
                "Music.Volume",
                float(value) / 100.0,
                actor_user_id=ctx.author.id,
            )
            await ctx.send(f"Default volume set to {value}%.")
        except Exception as e:
            await ctx.send(f":x: Failed: {e}")

    @discord.app_commands.command(name="repeat", description="Set repeat mode.")
    @discord.app_commands.describe(mode="Repeat mode")
    @discord.app_commands.choices(
        mode=[
            discord.app_commands.Choice(name="None", value="none"),
            discord.app_commands.Choice(name="Current Track", value="current"),
            discord.app_commands.Choice(name="Queue", value="queue"),
        ]
    )
    async def repeat(
        self, interaction: discord.Interaction, mode: discord.app_commands.Choice[str]
    ):
        player = self.bot.get_cog("MusicPlayer")
        player.repeat_mode[interaction.guild.id] = mode.value
        await interaction.response.send_message(f"Repeat mode set to: {mode.name}")


async def setup(bot):
    await bot.add_cog(ControlCommands(bot))
