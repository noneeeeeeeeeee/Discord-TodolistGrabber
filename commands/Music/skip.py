import discord
from discord.ext import commands

from modules.music.music_player import is_voice_playing


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
        name="skip",
        description="Skip current track or jump to queue position.",
        aliases=["s"],
    )
    async def skip(self, ctx: commands.Context, index: int = None):
        from modules.music.player_actions import handle_skip_action

        player = self.bot.get_cog("MusicPlayer")
        if not player:
            await ctx.send(":x: Player backend missing.")
            return

        is_valid, error_msg = player.check_user_in_bot_vc(ctx.author, ctx.guild)
        if not is_valid:
            await ctx.send(error_msg, delete_after=10)
            return

        if index is not None:
            if not self._is_dj(ctx):
                await ctx.send(":x: DJ/Admin required to skip to specific position.")
                return

            vc = ctx.guild.voice_client
            pomice_player = vc if isinstance(vc, __import__("pomice").Player) else None
            if not pomice_player or not hasattr(pomice_player, "queue"):
                await ctx.send(":x: Player is not ready.")
                return

            q = player.queues.get(ctx.guild.id)
            if not q or pomice_player.queue.is_empty:
                await ctx.send(":x: Queue is empty.")
                return

            if index < 1:
                await ctx.send(":x: Invalid index.")
                return

            # Remove tracks before the requested index from BOTH queues to keep them in sync.
            removed = 0
            for _ in range(index - 1):
                if pomice_player.queue.is_empty:
                    break
                try:
                    item = pomice_player.queue.get()
                    if __import__("asyncio").iscoroutine(item):
                        await item
                except Exception:
                    break
                if q:
                    try:
                        q.popleft()
                    except Exception:
                        pass
                removed += 1

            if removed == 0:
                await ctx.send(":x: Nothing to remove before that position.")
                return

            if vc and is_voice_playing(vc):
                await player.note_skip(
                    vc, ctx.guild.id, ctx.author.id, primary_listener_bias=True
                )
                await vc.stop()

            await ctx.send(f":fast_forward: Skipped to position {index}.")
            return

        # Regular skip (no index)
        vc = ctx.guild.voice_client
        if vc and is_voice_playing(vc):
            result = await handle_skip_action(
                player, ctx.guild, ctx.author, vc, ctx.channel
            )
            if result.get("embed"):
                await ctx.send(result.get("message", ""), embed=result["embed"])
            else:
                await ctx.send(result.get("message", ""))
            return

        # Not currently playing: allow DJs to remove the next queued track.
        if self._is_dj(ctx):
            q = player.queues.get(ctx.guild.id)
            if q:
                q.popleft()
                await ctx.send(":boom: Removed next queued track.")
                return

        await ctx.send("Nothing to skip.")


async def setup(bot):
    await bot.add_cog(SkipCommands(bot))
