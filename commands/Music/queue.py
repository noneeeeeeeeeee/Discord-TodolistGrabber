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
            lines = [
                self._format_entry(entry, idx)
                for idx, entry in enumerate(upcoming[:25], start=1)
            ]
            embed.add_field(
                name=f"⏭️ Up Next ({min(len(upcoming), 25)}/{len(upcoming)})",
                value="\n".join(lines),
                inline=False,
            )
            if len(upcoming) > 25:
                embed.set_footer(text=f"…and {len(upcoming) - 25} more item(s) queued.")
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

    @commands.hybrid_command(
        name="nowplaying",
        description="Show what's currently playing.",
        aliases=["np", "current"],
    )
    async def nowplaying(self, ctx: commands.Context):
        player = self.bot.get_cog("MusicPlayer")
        if not player:
            await ctx.send("Player backend not available.")
            return

        current = player._current_entries.get(ctx.guild.id)
        if not current:
            await ctx.send("Nothing is currently playing.")
            return

        title = current.get("title", "Unknown")
        uri = current.get("uri")
        requester_id = current.get("requester")
        track = current.get("track")

        # Create embed
        if uri:
            embed = discord.Embed(
                title=f"🎵 {title}",
                url=uri,
                color=discord.Color.blurple(),
            )
        else:
            embed = discord.Embed(
                title=f"🎵 {title}",
                color=discord.Color.blurple(),
            )

        # Add thumbnail if available
        if track:
            artwork_url = getattr(track, "artwork_url", None)
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

        # Add duration with seekbar
        if track:
            dur = getattr(track, "length", None)
            if dur:
                total_seconds = int(dur / 1000)
                minutes, seconds = divmod(total_seconds, 60)
                duration_str = f"{minutes:02d}:{seconds:02d}"

                # Visual seekbar
                seekbar_length = 15
                filled = 0
                empty = seekbar_length - filled
                seekbar = "🔘" + "▬" * empty

                embed.add_field(
                    name="Duration",
                    value=f"00:00 {seekbar} {duration_str}",
                    inline=False,
                )

        # Add requester footer
        if requester_id:
            try:
                requester = ctx.guild.get_member(requester_id)
                if requester:
                    embed.set_footer(
                        text=f"Requested by {requester.display_name}",
                        icon_url=requester.display_avatar.url,
                    )
                else:
                    embed.set_footer(text=f"Requested by User#{requester_id}")
            except Exception:
                embed.set_footer(text=f"Requested by <@{requester_id}>")

        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(QueueCommands(bot))
