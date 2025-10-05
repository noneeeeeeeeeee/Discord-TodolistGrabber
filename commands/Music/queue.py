import discord
from discord.ext import commands
from collections import deque
import random

from typing import Optional

from modules.music.music_player import is_voice_playing


class QueueCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="queue", description="Show current queue.", aliases=["q"]
    )
    async def queue(self, ctx: commands.Context, page: int = 1):
        player = self.bot.get_cog("MusicPlayer")
        if not player:
            await ctx.send("Player backend not available.")
            return

        current = player._current_entries.get(ctx.guild.id)
        upcoming = list(player.queues.get(ctx.guild.id, []))

        if not current and not upcoming:
            await ctx.send("Nothing is playing or queued.")
            return

        embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blurple())

        if current:
            # Add thumbnail from current track
            track_obj = current.get("track")
            if track_obj:
                artwork_url = getattr(track_obj, "artwork_url", None)
                uri = current.get("uri")
                if artwork_url:
                    embed.set_thumbnail(url=artwork_url)
                elif uri and "youtube.com/watch?v=" in uri:
                    video_id = uri.split("v=")[1].split("&")[0]
                    embed.set_thumbnail(
                        url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                    )
                elif uri and "youtu.be/" in uri:
                    video_id = uri.split("youtu.be/")[1].split("?")[0]
                    embed.set_thumbnail(
                        url=f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"
                    )

            embed.add_field(
                name="▶️ Now Playing",
                value=self._format_entry(current, None, is_current=True),
                inline=False,
            )

        if upcoming:
            # Pagination to handle Discord's 1024 character limit
            items_per_page = 10
            total_pages = max(1, (len(upcoming) + items_per_page - 1) // items_per_page)
            page = max(1, min(page, total_pages))  # Clamp page number

            start_idx = (page - 1) * items_per_page
            end_idx = min(start_idx + items_per_page, len(upcoming))

            # Build queue text with character limit safety
            lines = []
            char_count = 0
            for idx in range(start_idx, end_idx):
                entry = upcoming[idx]
                line = self._format_entry(entry, idx + 1)
                # Safety check: if adding this line would exceed 1000 chars, stop
                if char_count + len(line) + 1 > 1000:
                    break
                lines.append(line)
                char_count += len(line) + 1  # +1 for newline

            queue_text = "\n".join(lines) if lines else "*Page is empty*"

            embed.add_field(
                name=f"⏭️ Up Next (Page {page}/{total_pages}) • {len(upcoming)} total",
                value=queue_text,
                inline=False,
            )

            if total_pages > 1:
                embed.set_footer(text=f"Use /queue {page + 1} to see the next page")
        else:
            embed.add_field(
                name="⏭️ Up Next",
                value="*Queue empty*",
                inline=False,
            )

        repeat = player.repeat_mode.get(ctx.guild.id, "none").title()
        embed.add_field(name="🔁 Repeat Mode", value=repeat, inline=True)

        shuffle = "Enabled" if player.shuffle_flags.get(ctx.guild.id) else "Disabled"
        embed.add_field(name="🔀 Shuffle", value=shuffle, inline=True)

        autoplay_enabled = player.is_session_autoplay_enabled(ctx.guild.id)
        autoplay_status = "Enabled" if autoplay_enabled else "Disabled (session)"
        embed.add_field(name="🤖 AutoPlay", value=autoplay_status, inline=True)

        await ctx.send(embed=embed)

    @discord.app_commands.command(name="q", description="Show current queue.")
    async def queue_slash_alias(self, interaction: discord.Interaction):
        ctx = await commands.Context.from_interaction(interaction)
        ctx.command = self.queue
        await self.queue.callback(self, ctx)

    def _format_entry(
        self, entry: dict, index: Optional[int], is_current: bool = False
    ) -> str:
        title = entry.get("title") or "Unknown"
        uri = entry.get("uri")

        # Truncate title if too long
        max_title_length = 80
        if len(title) > max_title_length:
            title = title[: max_title_length - 3] + "..."

        # Format duration if available
        track_obj = entry.get("track")
        duration_str = ""
        if track_obj:
            dur = getattr(track_obj, "length", None)
            if dur:
                minutes, seconds = divmod(int(dur / 1000), 60)
                duration_str = f" `[{minutes}:{seconds:02d}]`"

        if uri:
            title = f"[{title}]({uri})"

        requester = entry.get("requester")
        if requester:
            who = f"<@{requester}>"
        else:
            who = "AutoPlay"

        marker = "🤖" if entry.get("autoplay") else "👤"

        if is_current:
            return f"**{title}**{duration_str}\nRequested by: {who}"
        else:
            prefix = f"`{index}.` " if index is not None else ""
            return f"{prefix}{marker} {title}{duration_str}"

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


async def setup(bot):
    await bot.add_cog(QueueCommands(bot))
