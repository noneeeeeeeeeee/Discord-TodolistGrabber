import discord
from discord.ext import commands


class ControlCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="pause", description="Pause playback.")
    async def pause(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await ctx.send("Paused playback.")
        else:
            await ctx.send("Nothing is playing.")

    @commands.hybrid_command(name="resume", description="Resume playback.")
    async def resume(self, ctx: commands.Context):
        vc = ctx.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await ctx.send("Resumed playback.")
        else:
            await ctx.send("Nothing to resume.")

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

    @commands.hybrid_command(
        name="repeat", description="Set repeat: none/current/queue."
    )
    async def repeat(self, ctx: commands.Context, mode: str):
        mode = mode.lower()
        if mode not in ("none", "current", "queue"):
            await ctx.send("Invalid repeat mode.")
            return
        player = self.bot.get_cog("MusicPlayer")
        player.repeat_mode[ctx.guild.id] = mode
        await ctx.send(f"Repeat mode set to: {mode}")


async def setup(bot):
    await bot.add_cog(ControlCommands(bot))
