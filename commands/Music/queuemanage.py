"""
Comprehensive queue management commands including clearqueue with flexible removal options.
"""

import discord
from discord.ext import commands
from collections import deque
import re
from typing import List, Set


def parse_removal_indices(input_str: str, queue_length: int) -> Set[int]:
    """
    Parse removal indices from user input like "1,2,5-10" or "1-2,4,2".
    Returns a set of 0-based indices. Duplicates are automatically ignored.

    Examples:
        "1,2,5-10" -> {0, 1, 4, 5, 6, 7, 8, 9}
        "1-2,4,2" -> {0, 1, 3}
    """
    indices = set()
    parts = input_str.split(",")

    for part in parts:
        part = part.strip()
        if "-" in part:
            # Range like "5-10"
            try:
                start, end = part.split("-", 1)
                start_idx = int(start.strip()) - 1  # Convert to 0-based
                end_idx = int(end.strip()) - 1

                # Validate range
                if start_idx < 0 or end_idx >= queue_length or start_idx > end_idx:
                    continue

                indices.update(range(start_idx, end_idx + 1))
            except ValueError:
                continue
        else:
            # Single number like "2"
            try:
                idx = int(part) - 1  # Convert to 0-based
                if 0 <= idx < queue_length:
                    indices.add(idx)
            except ValueError:
                continue

    return indices


class QueueManagementCommands(commands.Cog):
    """Queue management commands with advanced options."""

    def __init__(self, bot):
        self.bot = bot

    def _get_player(self):
        return self.bot.get_cog("MusicPlayer")

    @commands.hybrid_command(
        name="clearqueue",
        description="Clear entire queue or remove specific tracks.",
        aliases=["cq"],
    )
    async def clearqueue(self, ctx: commands.Context, *, indices: str = None):
        """
        Clear queue or remove specific tracks by index.

        Usage:
            /clearqueue - Clear entire queue
            /clearqueue 1,2,5-10 - Remove tracks 1, 2, and 5 through 10
            /clearqueue 1-2,4,2 - Remove tracks 1, 2, and 4 (duplicate 2 ignored)
        """
        player = self._get_player()
        if not player:
            await ctx.send("❌ Player backend not available.")
            return

        # Check DJ permissions or admin
        is_dj = await player._check_dj(ctx.author, ctx.guild)
        if not is_dj:
            if not (
                ctx.author.guild_permissions.administrator
                or ctx.author.guild_permissions.manage_guild
            ):
                # Requires voting
                vc = ctx.guild.voice_client
                if not vc:
                    await ctx.send("❌ Bot not in voice channel.")
                    return

                if not ctx.author.voice or ctx.author.voice.channel.id != vc.channel.id:
                    await ctx.send("❌ You must be in the same voice channel!")
                    return

                vote_result = await player.handle_vote_action(
                    ctx.guild.id, ctx.author.id, "clearqueue", vc.channel
                )

                if not vote_result["passed"]:
                    await ctx.send(
                        f"🗳️ Vote registered. Need {vote_result['needed']} votes to clear queue. "
                        f"({vote_result['votes']}/{vote_result['needed']})"
                    )
                    return

        queue = player.queues.get(ctx.guild.id, deque())

        if not queue:
            await ctx.send("❌ Queue is already empty.")
            return

        if indices is None:
            # Clear entire queue
            player.queues[ctx.guild.id].clear()
            await ctx.send("🗑️ Cleared entire queue.")
        else:
            # Parse and remove specific indices
            queue_list = list(queue)
            removal_set = parse_removal_indices(indices, len(queue_list))

            if not removal_set:
                await ctx.send("❌ No valid indices provided. Example: `1,2,5-10`")
                return

            # Create new queue without removed items
            new_queue = deque(
                [item for idx, item in enumerate(queue_list) if idx not in removal_set]
            )

            player.queues[ctx.guild.id] = new_queue
            removed_count = len(queue_list) - len(new_queue)
            await ctx.send(f"🗑️ Removed {removed_count} track(s) from queue.")

    @commands.hybrid_command(
        name="managequeue",
        description="Advanced queue management options.",
        aliases=["mq"],
    )
    @discord.app_commands.describe(action="Queue management action to perform")
    @discord.app_commands.choices(
        action=[
            discord.app_commands.Choice(
                name="Remove Duplicates", value="remove_duplicates"
            ),
            discord.app_commands.Choice(name="Reverse Queue", value="reverse"),
            discord.app_commands.Choice(
                name="Remove Absent Users' Songs", value="remove_absent"
            ),
            discord.app_commands.Choice(name="Sort by Title", value="sort_title"),
            discord.app_commands.Choice(
                name="Sort by Requester", value="sort_requester"
            ),
            discord.app_commands.Choice(name="Sort by Duration", value="sort_duration"),
        ]
    )
    async def managequeue(self, ctx: commands.Context, action: str):
        """
        Manage queue with various options.

        Actions:
        - remove_duplicates: Remove duplicate tracks
        - reverse: Reverse queue order
        - remove_absent: Remove tracks from users not in voice channel
        - sort_title: Sort queue alphabetically by title
        - sort_requester: Sort queue by requester
        - sort_duration: Sort queue by track duration
        """
        player = self._get_player()
        if not player:
            await ctx.send("❌ Player backend not available.")
            return

        # Check DJ permissions
        is_dj = await player._check_dj(ctx.author, ctx.guild)
        if not is_dj:
            if not (
                ctx.author.guild_permissions.administrator
                or ctx.author.guild_permissions.manage_guild
            ):
                await ctx.send("❌ DJ role or admin permissions required.")
                return

        queue = player.queues.get(ctx.guild.id, deque())

        if not queue:
            await ctx.send("❌ Queue is empty.")
            return

        if action == "remove_duplicates":
            seen = set()
            new_queue = deque()
            removed = 0
            for item in queue:
                key = item.get("title", "").lower()
                if key not in seen:
                    seen.add(key)
                    new_queue.append(item)
                else:
                    removed += 1
            player.queues[ctx.guild.id] = new_queue
            await ctx.send(f"🗑️ Removed {removed} duplicate track(s).")

        elif action == "reverse":
            player.queues[ctx.guild.id] = deque(list(queue)[::-1])
            await ctx.send("🔄 Queue reversed.")

        elif action == "remove_absent":
            vc = ctx.guild.voice_client
            if not vc:
                await ctx.send("❌ Bot not in voice channel.")
                return

            present_ids = {m.id for m in vc.channel.members if not m.bot}
            new_queue = deque(
                [item for item in queue if item.get("requester") in present_ids]
            )
            removed = len(queue) - len(new_queue)
            player.queues[ctx.guild.id] = new_queue
            await ctx.send(f"👤 Removed {removed} track(s) from absent users.")

        elif action == "sort_title":
            sorted_queue = sorted(queue, key=lambda x: x.get("title", "").lower())
            player.queues[ctx.guild.id] = deque(sorted_queue)
            await ctx.send("🔤 Queue sorted by title (A-Z).")

        elif action == "sort_requester":
            sorted_queue = sorted(queue, key=lambda x: x.get("requester", 0))
            player.queues[ctx.guild.id] = deque(sorted_queue)
            await ctx.send("👥 Queue sorted by requester.")

        elif action == "sort_duration":
            sorted_queue = sorted(queue, key=lambda x: x.get("duration", 0))
            player.queues[ctx.guild.id] = deque(sorted_queue)
            await ctx.send("⏱️ Queue sorted by duration (shortest first).")

        else:
            await ctx.send("❌ Invalid action specified.")

    @commands.hybrid_command(name="shuffle", description="Shuffle the queue randomly.")
    async def shuffle(self, ctx: commands.Context):
        """Shuffle the queue (only affects queue, not currently playing track)."""
        player = self._get_player()
        if not player:
            await ctx.send("❌ Player backend not available.")
            return

        # Check DJ permissions
        is_dj = await player._check_dj(ctx.author, ctx.guild)
        if not is_dj:
            if not (
                ctx.author.guild_permissions.administrator
                or ctx.author.guild_permissions.manage_guild
            ):
                # Requires voting
                vc = ctx.guild.voice_client
                if not vc:
                    await ctx.send("❌ Bot not in voice channel.")
                    return

                if not ctx.author.voice or ctx.author.voice.channel.id != vc.channel.id:
                    await ctx.send("❌ You must be in the same voice channel!")
                    return

                vote_result = await player.handle_vote_action(
                    ctx.guild.id, ctx.author.id, "shuffle", vc.channel
                )

                if not vote_result["passed"]:
                    await ctx.send(
                        f"🗳️ Vote registered. Need {vote_result['needed']} votes to shuffle. "
                        f"({vote_result['votes']}/{vote_result['needed']})"
                    )
                    return

        queue = list(player.queues.get(ctx.guild.id, []))

        if not queue:
            await ctx.send("❌ Queue is empty.")
            return

        import random

        random.shuffle(queue)
        player.queues[ctx.guild.id] = deque(queue)
        player.shuffle_flags[ctx.guild.id] = True

        await ctx.send(f"🔀 Shuffled {len(queue)} track(s) in queue.")


async def setup(bot):
    await bot.add_cog(QueueManagementCommands(bot))
