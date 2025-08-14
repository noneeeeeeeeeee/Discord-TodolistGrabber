import discord
from discord.ext import commands
from collections import deque
import random


class QueueCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="queue", description="Show current queue.")
    async def queue(self, ctx: commands.Context):
        player = self.bot.get_cog("MusicPlayer")
        if not player:
            await ctx.send("Player backend not available.")
            return
        q = list(player.queues.get(ctx.guild.id, []))
        if not q:
            await ctx.send("Queue is empty.")
            return
        lines = [
            f"{i+1}. {it.get('title')} — <@{it.get('requester')}>"
            for i, it in enumerate(q[:25])
        ]
        await ctx.send(embed=discord.Embed(title="Queue", description="\n".join(lines)))

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
