import discord
from discord.ext import commands


class SkipCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_dj(self, ctx):
        cfg = __import__("modules.setconfig", fromlist=["json_get"]).json_get(
            ctx.guild.id
        )
        dj_role = cfg.get("Music", {}).get("DJRole")
        if dj_role:
            try:
                return any(r.id == int(dj_role) for r in ctx.author.roles)
            except Exception:
                pass
        return (
            ctx.author.guild_permissions.manage_guild
            or ctx.author.guild_permissions.administrator
        )

    @commands.hybrid_command(
        name="skip", description="Skip the current track (DJ or voteskip)."
    )
    async def skip(self, ctx: commands.Context):
        player = self.bot.get_cog("MusicPlayer")
        if not player:
            await ctx.send("Player backend missing.")
            return

        if self._is_dj(ctx):
            vc = ctx.guild.voice_client
            if vc and vc.is_playing():
                vc.stop()
                await ctx.send("Skipped by DJ.")
                return
            q = player.queues.get(ctx.guild.id)
            if q:
                q.popleft()
                await ctx.send("Removed next queued track.")
                return
            await ctx.send("Nothing to skip.")
            return

        # voteskip
        added, cur, needed = await player.handle_vote_skip(ctx.guild, ctx.author.id)
        if not added:
            await ctx.send("You already voted to skip.")
            return
        if cur >= needed:
            vc = ctx.guild.voice_client
            if vc and vc.is_playing():
                vc.stop()
            player.voteskip[ctx.guild.id].clear()
            await ctx.send("Vote threshold reached. Skipping track.")
        else:
            await ctx.send(f"Voted to skip ({cur}/{needed}).")


async def setup(bot):
    await bot.add_cog(SkipCommands(bot))
