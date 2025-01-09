import discord
from discord.ext import commands
from modules.setconfig import json_get


class Skip(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(
        name="skip",
        aliases=["next", "s"],
        description="Skips the currently playing song or multiple songs.",
    )
    @discord.app_commands.describe(count="Number of songs to skip")
    async def skip(self, ctx: commands.Context, count: int = 1):
        """Skips the currently playing song or multiple songs."""
        voice_client = ctx.guild.voice_client
        music_player = self.bot.get_cog("MusicPlayer")

        if voice_client and voice_client.is_playing():
            guild_id = ctx.guild.id
            skipped_songs = 0

            for _ in range(count):
                if (
                    guild_id in music_player.music_queue
                    and music_player.music_queue[guild_id]
                ):
                    voice_client.stop()
                    skipped_songs += 1
                else:
                    break

            if skipped_songs > 0:
                await ctx.send(f":fast_forward: Skipped {skipped_songs} song(s).")
            else:
                await ctx.send(":x: No more songs in the queue.")
        else:
            await ctx.send(":x: Nothing is currently playing.")


async def setup(bot):
    await bot.add_cog(Skip(bot))
