import discord
from discord.ext import commands
from collections import deque
import random

from typing import Optional


class QueueCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="queue", description="Show current queue.")
    async def queue(self, ctx: commands.Context):
        player = self.bot.get_cog("MusicPlayer")
        if not player:
            await ctx.send("Player backend not available.")
            return

        current = player._current_entries.get(ctx.guild.id)
        upcoming = list(player.queues.get(ctx.guild.id, []))

        if not current and not upcoming:
            await ctx.send("Nothing is playing or queued.")
            return

        embed = discord.Embed(title="Music Queue", color=discord.Color.blurple())

        if current:
            embed.add_field(
                name="Now Playing",
                value=self._format_entry(current, None),
                inline=False,
            )

        if upcoming:
            lines = [
                self._format_entry(entry, idx)
                for idx, entry in enumerate(upcoming[:25], start=1)
            ]
            embed.add_field(
                name=f"Up Next ({min(len(upcoming), 25)}/{len(upcoming)})",
                value="\n".join(lines),
                inline=False,
            )
            if len(upcoming) > 25:
                embed.set_footer(text=f"…and {len(upcoming) - 25} more item(s) queued.")
        else:
            embed.add_field(
                name="Up Next",
                value="Queue empty",
                inline=False,
            )

        repeat = player.repeat_mode.get(ctx.guild.id, "none").title()
        embed.add_field(name="Repeat Mode", value=repeat, inline=True)

        shuffle = "Enabled" if player.shuffle_flags.get(ctx.guild.id) else "Disabled"
        embed.add_field(name="Shuffle", value=shuffle, inline=True)

        await ctx.send(embed=embed)

    def _format_entry(self, entry: dict, index: Optional[int]) -> str:
        title = entry.get("title") or "Unknown"
        uri = entry.get("uri")
        if uri:
            title = f"[{title}]({uri})"

        requester = entry.get("requester")
        if requester:
            who = f"<@{requester}>"
        else:
            who = "AutoPlay"

        marker = "AUTO" if entry.get("autoplay") else "REQ"
        prefix = f"{index}. " if index is not None else ""
        return f"{prefix}[{marker}] {title} — {who}"

    @commands.hybrid_command(
        name="shuffle",
        description="Shuffle the queue and persist the flag for session.",
    )
    async def shuffle(self, ctx: commands.Context):
        if not (
            ctx.author.guild_permissions.manage_guild
            or ctx.author.guild_permissions.administrator
        ):
            await ctx.send(":x: Admin or DJ required.")
            return
        player = self.bot.get_cog("MusicPlayer")
        q = list(player.queues.get(ctx.guild.id, []))
        random.shuffle(q)
        player.queues[ctx.guild.id] = deque(q)
        player.shuffle_flags[ctx.guild.id] = True
        await ctx.send("Queue shuffled and shuffle persisted for session.")

    @commands.hybrid_command(name="reverse", description="Reverse queue order.")
    async def reverse(self, ctx: commands.Context):
        player = self.bot.get_cog("MusicPlayer")
        q = list(player.queues.get(ctx.guild.id, []))[::-1]
        player.queues[ctx.guild.id] = deque(q)
        await ctx.send("Queue reversed.")

    @commands.hybrid_command(
        name="removeduplicates", description="Remove duplicates from queue."
    )
    async def removeduplicates(self, ctx: commands.Context):
        player = self.bot.get_cog("MusicPlayer")
        q = player.queues.get(ctx.guild.id, deque())
        seen = set()
        newq = deque()
        removed = 0
        for it in q:
            key = it.get("title", "").lower()
            if key in seen:
                removed += 1
                continue
            seen.add(key)
            newq.append(it)
        player.queues[ctx.guild.id] = newq
        await ctx.send(f"Removed {removed} duplicate(s).")

    @commands.hybrid_command(name="skipto", description="Skip to index in queue.")
    async def skipto(self, ctx: commands.Context, index: int):
        player = self.bot.get_cog("MusicPlayer")
        q = player.queues.get(ctx.guild.id, deque())
        if index < 1 or index > len(q):
            await ctx.send(":x: Invalid index.")
            return
        for _ in range(index - 1):
            q.popleft()
        vc = ctx.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
        await ctx.send(f"Skipped to {index}.")


async def setup(bot):
    await bot.add_cog(QueueCommands(bot))
