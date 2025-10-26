import os
import inspect
import discord
from discord.ext import commands
from pathlib import Path


def _is_owner(user_id: int) -> bool:
    """Check if user is the bot owner."""
    try:
        return str(user_id) == str(os.getenv("OWNER_ID") or "")
    except Exception:
        return False


class AutoPlaySettings(commands.Cog):
    """Admin commands for Last.fm AutoPlay management."""

    def __init__(self, bot):
        self.bot = bot

    def _get_player(self):
        """Get the MusicPlayer cog."""
        return self.bot.get_cog("MusicPlayer")

    @commands.hybrid_command(
        name="clearlfmcache",
        description="[OWNER ONLY] Clear Last.fm mapping cache. Use only if cache has bad mappings.",
    )
    async def clearlfmcache(self, ctx: commands.Context):
        """
        Clear the Last.fm mapping cache.

        ⚠️ WARNING: This will make recommendations load slower initially as the cache rebuilds.
        Only use this if you have bad/incorrect track mappings in the cache.

        This command is restricted to the bot owner only.
        """
        # Check if user is owner
        if not _is_owner(ctx.author.id):
            await ctx.send(":x: This command is restricted to the bot owner only.")
            return

        # Get player to access Last.fm autoplay
        player = self._get_player()
        if not player or not getattr(player, "_lastfm_autoplay", None):
            await ctx.send(":x: Last.fm AutoPlay is not available.")
            return

        # Send warning and confirmation
        embed = discord.Embed(
            title="⚠️ Clear Last.fm Cache?",
            description=(
                "This will delete all Last.fm → YouTube track mappings.\n\n"
                "**Effects:**\n"
                "• Recommendations will load slower initially\n"
                "• Bad/incorrect mappings will be cleared\n"
                "• Cache will rebuild as tracks are played\n\n"
                "**When to use:**\n"
                "• Track mappings are incorrect (wrong songs)\n"
                "• AutoPlay is playing unofficial/cover versions\n"
                "• Cache is corrupted\n\n"
                "React with ✅ to confirm or ❌ to cancel."
            ),
            color=discord.Color.orange(),
        )

        msg = await ctx.send(embed=embed)
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")

        def check(reaction, user):
            return (
                user == ctx.author
                and str(reaction.emoji) in ["✅", "❌"]
                and reaction.message.id == msg.id
            )

        try:
            reaction, user = await self.bot.wait_for(
                "reaction_add", timeout=30.0, check=check
            )

            if str(reaction.emoji) == "✅":
                # Clear the cache
                try:
                    # Support both V1 and V2 mapping cache files
                    mapping_files = [
                        Path("cache/music/lastfm_mappings_v1.json"),
                        Path("cache/music/mappings_v2.json"),
                    ]
                    total_deleted = 0
                    total_entries = 0

                    import json

                    for fp in mapping_files:
                        if fp.exists():
                            try:
                                with fp.open("r", encoding="utf-8") as f:
                                    data = json.load(f)
                                    if isinstance(data, dict):
                                        total_entries += len(data)
                                    elif isinstance(data, list):
                                        total_entries += len(data)
                            except Exception:
                                pass
                            try:
                                fp.unlink()
                                total_deleted += 1
                            except Exception:
                                pass

                    # Attempt to clear in-memory caches for current engine if supported
                    try:
                        # V2 path: MusicPlayer._lastfm_autoplay is a LastFMAutoplayV2 orchestrator
                        orchestrator = getattr(player, "_lastfm_autoplay", None)
                        engine = getattr(orchestrator, "_engine", None)
                        cache = getattr(engine, "_cache", None)
                        clear_fn = getattr(cache, "clear_mappings", None)
                        if callable(clear_fn):
                            result = clear_fn()
                            if inspect.isawaitable(result):
                                await result
                    except Exception:
                        # V1 path: try legacy attribute if present
                        try:
                            legacy_cache = getattr(player._lastfm_autoplay, "_cache", None)
                            if legacy_cache and hasattr(legacy_cache, "clear"):
                                legacy_cache.clear()
                        except Exception:
                            pass

                    success_embed = discord.Embed(
                        title="✅ Cache Cleared",
                        description=(
                            f"Successfully cleared Last.fm mapping cache.\n\n"
                            f"**Cleared files:** {total_deleted} (V1/V2)\n"
                            f"**Estimated entries removed:** {total_entries}+\n"
                            f"**Status:** Cache will rebuild as tracks are played"
                        ),
                        color=discord.Color.green(),
                    )
                    await msg.edit(embed=success_embed)

                except Exception as e:
                    error_embed = discord.Embed(
                        title="❌ Error",
                        description=f"Failed to clear cache: {e}",
                        color=discord.Color.red(),
                    )
                    await msg.edit(embed=error_embed)

            else:
                cancel_embed = discord.Embed(
                    title="❌ Cancelled",
                    description="Cache clear operation cancelled.",
                    color=discord.Color.red(),
                )
                await msg.edit(embed=cancel_embed)

        except TimeoutError:
            timeout_embed = discord.Embed(
                title="⏱️ Timeout",
                description="Cache clear operation timed out (no response).",
                color=discord.Color.red(),
            )
            await msg.edit(embed=timeout_embed)


async def setup(bot):
    await bot.add_cog(AutoPlaySettings(bot))
