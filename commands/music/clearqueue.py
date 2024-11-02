import discord
from discord.ext import commands

class ClearQueue(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command(name="clearqueue", aliases=["clear", "cq"])
    async def clear_queue(self, ctx):
        """Clears the music queue."""
        music_player = self.bot.get_cog("MusicPlayer")
        disconnect_state = self.bot.get_cog("Disconnect").disconnect_state
        guild_id = ctx.guild.id

        if guild_id in music_player.music_queue:
            music_player.music_queue[guild_id].clear()
            if not disconnect_state.is_intentional():
                await ctx.send(":white_check_mark: The queue has been cleared.")
        else:
            if not disconnect_state.is_intentional():
                await ctx.send(":x: The queue is already empty.")

    async def clear_queue_items(self, ctx, *, items: str):
        """Clears specific items from the music queue based on their positions."""
        music_player = self.bot.get_cog("MusicPlayer")
        guild_id = ctx.guild.id

        if guild_id not in music_player.music_queue or not music_player.music_queue[guild_id]:
            await ctx.send(":x: The queue is currently empty.")
            return

        queue = music_player.music_queue[guild_id]
        indices = [int(i) - 1 for i in items.split(",") if i.isdigit()]
        indices = sorted(set(indices), reverse=True) 

        deleted_songs = []
        for index in indices:
            if 0 <= index < len(queue):
                song = queue.pop(index)
                deleted_songs.append(f"{index + 1}. {song['title']}")  

        if not deleted_songs:
            await ctx.send(":x: No valid positions provided or positions out of range.")
        elif len(deleted_songs) == 1:
            await ctx.send(f":white_check_mark: Successfully Deleted {deleted_songs[0]}")
        else:
            await ctx.send(":white_check_mark: Successfully Deleted Songs Shown Below:\n" + "\n".join(deleted_songs))

async def setup(bot):
    await bot.add_cog(ClearQueue(bot))
