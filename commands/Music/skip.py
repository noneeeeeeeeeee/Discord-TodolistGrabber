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
        player = self.bot.get_cog("MusicPlayer")
        if not player:
            await ctx.send("Player backend missing.")
            return

        is_valid, error_msg = player.check_user_in_bot_vc(ctx.author, ctx.guild)
        if not is_valid:
            await ctx.send(error_msg, delete_after=10)
            return

        if index is not None:
            if not self._is_dj(ctx):
                await ctx.send(":x: DJ/Admin required to skip to specific position.")
                return

            q = player.queues.get(ctx.guild.id)
            if not q:
                await ctx.send(":x: Queue is empty.")
                return

            if index < 1 or index > len(q):
                await ctx.send(f":x: Invalid index. Queue has {len(q)} tracks.")
                return

            # Skip to index by removing all tracks before it
            for _ in range(index - 1):
                q.popleft()

            vc = ctx.guild.voice_client
            if vc and is_voice_playing(vc):
                await player.note_skip(
                    vc, ctx.guild.id, ctx.author.id, primary_listener_bias=True
                )
                await vc.stop()

            await ctx.send(f"Skipped to position {index}.")
            return

        # Regular skip (no index)
        if self._is_dj(ctx):
            vc = ctx.guild.voice_client
            if vc and is_voice_playing(vc):
                await player.note_skip(
                    vc, ctx.guild.id, ctx.author.id, primary_listener_bias=True
                )
                await vc.stop()
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
            await ctx.send("You already voted to skip this track.")
            return

        remaining = max(0, needed - cur)
        if cur >= needed:
            vc = ctx.guild.voice_client
            player.voteskip[ctx.guild.id].clear()

            # Log queue state before skip
            pomice_player = vc if isinstance(vc, __import__("pomice").Player) else None
            queue_size = 0
            if pomice_player and hasattr(pomice_player, "queue"):
                queue_size = (
                    len(pomice_player.queue._queue)
                    if hasattr(pomice_player.queue, "_queue")
                    else 0
                )

            print(
                f"[VOTESKIP] Vote passed for guild {ctx.guild.id}. Queue size: {queue_size}"
            )

            if vc and is_voice_playing(vc):
                await player.note_skip(
                    vc, ctx.guild.id, ctx.author.id, primary_listener_bias=False
                )
                print(f"[VOTESKIP] Calling vc.stop() for guild {ctx.guild.id}")
                await vc.stop()
                print(f"[VOTESKIP] vc.stop() completed for guild {ctx.guild.id}")
            else:
                print(
                    f"[VOTESKIP] WARNING: vc is None or not playing (guild={ctx.guild.id}, vc={vc}, playing={is_voice_playing(vc) if vc else False})"
                )

            await ctx.send(
                embed=discord.Embed(
                    title="Skip Vote",
                    description="Threshold reached – skipping now!",
                    color=discord.Color.green(),
                )
            )
        else:
            status = f"{cur}/{needed} votes. {remaining} more required to skip."
            await ctx.send(
                embed=discord.Embed(
                    title="Skip Vote",
                    description=status,
                    color=discord.Color.orange(),
                )
            )


async def setup(bot):
    await bot.add_cog(SkipCommands(bot))
