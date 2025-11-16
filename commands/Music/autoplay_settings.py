import os
import inspect
import discord
from discord.ext import commands
from pathlib import Path
import json
import io
from datetime import datetime


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

    @commands.hybrid_command(
        name="refreshingestqueue",
        description="[OWNER ONLY] Rebuild the AutoPlay ingest queue from cached tracks.",
    )
    async def refresh_ingest_queue(self, ctx: commands.Context, limit: int = None):
        if not _is_owner(ctx.author.id):
            await ctx.send(":x: This command is restricted to the bot owner only.")
            return

        player = self._get_player()
        orchestrator = getattr(player, "_lastfm_autoplay", None) if player else None
        if not orchestrator or not hasattr(orchestrator, "refresh_ingest_queue"):
            await ctx.send(":x: AutoPlay ingest helpers are unavailable.")
            return

        await ctx.defer()
        try:
            stats = await orchestrator.refresh_ingest_queue(limit=limit)
        except Exception as exc:
            await ctx.send(f":x: Failed to refresh ingest queue: {exc}")
            return

        embed = discord.Embed(
            title="🔁 Ingest Queue Refreshed",
            description="Rebuilt pending analysis jobs from cached enrichment entries.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Queued", value=str(stats.get("queued", 0)))
        embed.add_field(name="Missing Mapping", value=str(stats.get("missing_mapping", 0)))
        embed.add_field(name="Duplicates", value=str(stats.get("duplicates", 0)))
        embed.add_field(name="Queue Depth", value=str(stats.get("queue_depth", 0)))
        if limit:
            embed.set_footer(text=f"Limit applied: {limit}")
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="reingest",
        description="[OWNER ONLY] Force AutoPlay to re-enrich and analyze a specific track.",
    )
    async def reingest_track(
        self,
        ctx: commands.Context,
        artist: str,
        title: str,
        youtube_id: str = None,
    ):
        if not _is_owner(ctx.author.id):
            await ctx.send(":x: This command is restricted to the bot owner only.")
            return

        player = self._get_player()
        orchestrator = getattr(player, "_lastfm_autoplay", None) if player else None
        if not orchestrator or not hasattr(orchestrator, "reingest_track"):
            await ctx.send(":x: AutoPlay ingest helpers are unavailable.")
            return

        await ctx.defer()
        try:
            result = await orchestrator.reingest_track(artist, title, youtube_id=youtube_id)
        except Exception as exc:
            await ctx.send(f":x: Failed to re-ingest track: {exc}")
            return

        color = discord.Color.green() if result.get("queued_analysis") else discord.Color.orange()
        embed = discord.Embed(
            title="🎧 Track Re-ingest Result",
            color=color,
        )
        embed.add_field(name="Artist", value=artist, inline=True)
        embed.add_field(name="Title", value=title, inline=True)
        embed.add_field(
            name="Enriched",
            value="✅" if result.get("enriched") else "❌",
            inline=True,
        )
        embed.add_field(
            name="Queued Analysis",
            value="✅" if result.get("queued_analysis") else "⚠️",
            inline=True,
        )
        embed.add_field(
            name="Mapping Found",
            value="✅" if result.get("mapping_found") else "❌",
            inline=True,
        )
        youtube_source = result.get("youtube_source") or "Unavailable"
        embed.add_field(name="YouTube Source", value=str(youtube_source), inline=False)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="autoplayexport",
        description="[OWNER ONLY] Export AutoPlay preferences (history, feedback, cache) to JSON.",
    )
    async def autoplay_export(self, ctx: commands.Context):
        """
        Export all AutoPlay data to a JSON file.
        
        Exports:
        • Last.fm → YouTube track mappings (cache)
        • Track enrichment data (genres, moods, parsing)
        • Telemetry and feedback events
        • Mood vectors
        
        This allows you to backup preferences and restore them later.
        """
        if not _is_owner(ctx.author.id):
            await ctx.send(":x: This command is restricted to the bot owner only.")
            return

        await ctx.defer()

        try:
            export_data = {
                "version": "2.0",
                "exported_at": datetime.utcnow().isoformat(),
                "bot_id": str(self.bot.user.id) if self.bot.user else None,
                "data": {}
            }

            # Export V2 cache files
            cache_files = {
                "mappings": Path("cache/music/mappings_v2.json"),
                "enrichment": Path("cache/music/enrichment_v2.json"),
                "parsing": Path("cache/music/parsing_v2.json"),
                "mood_vectors": Path("cache/music/mood_vectors_v2.json"),
                "track_metadata": Path("cache/music/track_metadata_v1.json"),
                "lastfm_mappings": Path("cache/music/lastfm_mappings_v1.json"),
            }

            for key, filepath in cache_files.items():
                if filepath.exists():
                    try:
                        with filepath.open("r", encoding="utf-8") as f:
                            export_data["data"][key] = json.load(f)
                    except Exception as e:
                        export_data["data"][key] = {"error": f"Failed to load: {e}"}

            # Export telemetry events (last 1000 entries to avoid huge files)
            telemetry_file = Path("cache/music/telemetry/events.jsonl")
            if telemetry_file.exists():
                try:
                    events = []
                    with telemetry_file.open("r", encoding="utf-8") as f:
                        for line in f:
                            try:
                                events.append(json.loads(line.strip()))
                            except:
                                pass
                    # Keep last 1000 events
                    export_data["data"]["telemetry_events"] = events[-1000:]
                except Exception as e:
                    export_data["data"]["telemetry_events"] = {"error": f"Failed to load: {e}"}

            # Export Gemini usage stats
            gemini_usage_file = Path("cache/music/gemini_usage.json")
            if gemini_usage_file.exists():
                try:
                    with gemini_usage_file.open("r", encoding="utf-8") as f:
                        export_data["data"]["gemini_usage"] = json.load(f)
                except Exception as e:
                    export_data["data"]["gemini_usage"] = {"error": f"Failed to load: {e}"}

            # Create JSON file
            json_str = json.dumps(export_data, indent=2, ensure_ascii=False)
            file_buffer = io.BytesIO(json_str.encode("utf-8"))
            file_buffer.seek(0)

            # Send as file attachment
            filename = f"autoplay_export_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
            file = discord.File(file_buffer, filename=filename)

            embed = discord.Embed(
                title="✅ AutoPlay Data Exported",
                description=(
                    f"**Exported data:**\n"
                    f"• Track mappings and cache\n"
                    f"• Enrichment data (genres, moods)\n"
                    f"• Telemetry events (last 1000)\n"
                    f"• Gemini usage stats\n\n"
                    f"**File size:** {len(json_str) / 1024:.1f} KB\n"
                    f"**To restore:** Use `/autoplayimport` with this file"
                ),
                color=discord.Color.green(),
            )

            await ctx.send(embed=embed, file=file)

        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Export Failed",
                description=f"Failed to export AutoPlay data: {e}",
                color=discord.Color.red(),
            )
            await ctx.send(embed=error_embed)

    @commands.hybrid_command(
        name="autoplayimport",
        description="[OWNER ONLY] Import AutoPlay preferences from a JSON file.",
    )
    async def autoplay_import(self, ctx: commands.Context):
        """
        Import AutoPlay data from a previously exported JSON file.
        
        **How to use:**
        1. Run this command
        2. Upload the exported JSON file when prompted
        3. Confirm the import operation
        
        ⚠️ This will overwrite existing cache data!
        """
        if not _is_owner(ctx.author.id):
            await ctx.send(":x: This command is restricted to the bot owner only.")
            return

        # Ask user to upload file
        prompt_embed = discord.Embed(
            title="📂 Upload Import File",
            description=(
                "Please upload the AutoPlay export JSON file.\n\n"
                "**Requirements:**\n"
                "• Must be a `.json` file\n"
                "• Must be from `/autoplayexport` command\n"
                "• File size < 8 MB\n\n"
                "You have **60 seconds** to upload the file."
            ),
            color=discord.Color.blue(),
        )
        await ctx.send(embed=prompt_embed)

        def check(message):
            return (
                message.author == ctx.author
                and message.channel == ctx.channel
                and len(message.attachments) > 0
            )

        try:
            msg = await self.bot.wait_for("message", timeout=60.0, check=check)
            attachment = msg.attachments[0]

            if not attachment.filename.endswith(".json"):
                await ctx.send("❌ File must be a `.json` file.")
                return

            if attachment.size > 8 * 1024 * 1024:  # 8 MB limit
                await ctx.send("❌ File is too large (max 8 MB).")
                return

            # Download and parse JSON
            file_bytes = await attachment.read()
            import_data = json.loads(file_bytes.decode("utf-8"))

            # Validate structure
            if "data" not in import_data:
                await ctx.send("❌ Invalid export file format.")
                return

            # Show confirmation prompt
            data_keys = list(import_data.get("data", {}).keys())
            confirm_embed = discord.Embed(
                title="⚠️ Confirm Import",
                description=(
                    f"**Import preview:**\n"
                    f"• Export version: {import_data.get('version', 'unknown')}\n"
                    f"• Exported at: {import_data.get('exported_at', 'unknown')}\n"
                    f"• Data sections: {len(data_keys)}\n\n"
                    f"**Sections to import:**\n"
                    + "\n".join(f"  • {key}" for key in data_keys[:10])
                    + (f"\n  • ...and {len(data_keys) - 10} more" if len(data_keys) > 10 else "")
                    + "\n\n⚠️ **This will overwrite existing cache!**\n"
                    "React with ✅ to confirm or ❌ to cancel."
                ),
                color=discord.Color.orange(),
            )

            confirm_msg = await ctx.send(embed=confirm_embed)
            await confirm_msg.add_reaction("✅")
            await confirm_msg.add_reaction("❌")

            def reaction_check(reaction, user):
                return (
                    user == ctx.author
                    and str(reaction.emoji) in ["✅", "❌"]
                    and reaction.message.id == confirm_msg.id
                )

            reaction, user = await self.bot.wait_for(
                "reaction_add", timeout=30.0, check=reaction_check
            )

            if str(reaction.emoji) != "✅":
                cancel_embed = discord.Embed(
                    title="❌ Import Cancelled",
                    description="Import operation cancelled by user.",
                    color=discord.Color.red(),
                )
                await confirm_msg.edit(embed=cancel_embed)
                return

            # Perform import
            imported_count = 0
            failed_count = 0

            cache_file_mapping = {
                "mappings": Path("cache/music/mappings_v2.json"),
                "enrichment": Path("cache/music/enrichment_v2.json"),
                "parsing": Path("cache/music/parsing_v2.json"),
                "mood_vectors": Path("cache/music/mood_vectors_v2.json"),
                "track_metadata": Path("cache/music/track_metadata_v1.json"),
                "lastfm_mappings": Path("cache/music/lastfm_mappings_v1.json"),
                "gemini_usage": Path("cache/music/gemini_usage.json"),
            }

            for key, filepath in cache_file_mapping.items():
                if key in import_data["data"] and not isinstance(import_data["data"][key], dict) or "error" not in import_data["data"][key]:
                    try:
                        filepath.parent.mkdir(parents=True, exist_ok=True)
                        with filepath.open("w", encoding="utf-8") as f:
                            json.dump(import_data["data"][key], f, indent=2, ensure_ascii=False)
                        imported_count += 1
                    except Exception as e:
                        failed_count += 1

            # Import telemetry events
            if "telemetry_events" in import_data["data"]:
                try:
                    telemetry_file = Path("cache/music/telemetry/events.jsonl")
                    telemetry_file.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Append to existing telemetry (don't overwrite)
                    with telemetry_file.open("a", encoding="utf-8") as f:
                        for event in import_data["data"]["telemetry_events"]:
                            f.write(json.dumps(event, ensure_ascii=False) + "\n")
                    imported_count += 1
                except Exception:
                    failed_count += 1

            success_embed = discord.Embed(
                title="✅ Import Complete",
                description=(
                    f"**Import results:**\n"
                    f"• Successfully imported: {imported_count} sections\n"
                    f"• Failed: {failed_count} sections\n\n"
                    f"**Status:** Cache has been restored from backup."
                ),
                color=discord.Color.green(),
            )
            await confirm_msg.edit(embed=success_embed)

        except TimeoutError:
            timeout_embed = discord.Embed(
                title="⏱️ Timeout",
                description="Import operation timed out (no file uploaded).",
                color=discord.Color.red(),
            )
            await ctx.send(embed=timeout_embed)
        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Import Failed",
                description=f"Failed to import AutoPlay data: {e}",
                color=discord.Color.red(),
            )
            await ctx.send(embed=error_embed)

    @commands.hybrid_command(
        name="autoplayfixlowquality",
        description="[OWNER ONLY] Scan and fix low-quality mappings in cache using updated heuristics.",
    )
    async def autoplay_fix_low_quality(self, ctx: commands.Context):
        """
        Scan cache for low-quality track mappings and fix them.
        
        This command:
        • Scans all cached track mappings
        • Uses updated heuristics to detect spam/reuploads
        • Removes low-quality entries
        • Preserves official/verified tracks
        
        Useful when heuristics are updated and you don't want to clear entire cache.
        """
        if not _is_owner(ctx.author.id):
            await ctx.send(":x: This command is restricted to the bot owner only.")
            return

        await ctx.defer()

        try:
            # Import heuristics from track_resolver
            from modules.music.Autoplay_Engine.v3.track_resolver import (
                BAD_TITLE_KEYWORDS,
                GOOD_CHANNEL_HINTS,
                GOOD_TITLE_KEYWORDS,
            )

            def is_low_quality(track_info: dict) -> tuple[bool, str]:
                """Check if track is low quality using heuristics."""
                channel = (track_info.get("uploader") or track_info.get("channel") or "").lower()
                title = (track_info.get("title") or "").lower()
                
                # Check for verified/official channels (KEEP THESE)
                for hint in GOOD_CHANNEL_HINTS:
                    if hint in channel:
                        return False, "verified_channel"
                
                # Check for official titles (KEEP THESE)
                for keyword in GOOD_TITLE_KEYWORDS:
                    if keyword in title:
                        return False, "official_title"
                
                # Check for bad keywords (REMOVE THESE)
                bad_matches = []
                for keyword in BAD_TITLE_KEYWORDS:
                    if keyword in title:
                        bad_matches.append(keyword)
                
                if bad_matches:
                    return True, f"spam_keywords: {', '.join(bad_matches[:3])}"
                
                # Check for suspicious patterns
                if any(x in title for x in ["#shorts", "#fyp", "#edit"]):
                    return True, "shorts_spam"
                
                if any(x in channel for x in ["fan", "reupload", "cover", "nightcore"]):
                    return True, "reupload_channel"
                
                return False, "clean"

            # Scan cache files
            cache_files = {
                "V2 Mappings": Path("cache/music/mappings_v2.json"),
                "V1 Last.fm": Path("cache/music/lastfm_mappings_v1.json"),
            }

            total_scanned = 0
            total_removed = 0
            removal_reasons = {}

            for cache_name, filepath in cache_files.items():
                if not filepath.exists():
                    continue

                with filepath.open("r", encoding="utf-8") as f:
                    data = json.load(f)

                if not isinstance(data, dict):
                    continue

                original_count = len(data)
                keys_to_remove = []

                for key, value in data.items():
                    total_scanned += 1
                    
                    # Extract track info (handle different cache formats)
                    track_info = {}
                    if isinstance(value, dict):
                        if "track" in value:
                            track_info = value.get("track", {})
                        else:
                            track_info = value
                    
                    if not track_info:
                        continue

                    is_bad, reason = is_low_quality(track_info)
                    if is_bad:
                        keys_to_remove.append(key)
                        removal_reasons[reason] = removal_reasons.get(reason, 0) + 1

                # Remove bad entries
                for key in keys_to_remove:
                    del data[key]
                    total_removed += 1

                # Write back to file if changes were made
                if keys_to_remove:
                    with filepath.open("w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2, ensure_ascii=False)

            # Create summary embed
            if total_removed > 0:
                reasons_str = "\n".join(
                    f"  • {reason}: {count}" 
                    for reason, count in sorted(removal_reasons.items(), key=lambda x: -x[1])[:5]
                )
                
                embed = discord.Embed(
                    title="✅ Low Quality Mappings Removed",
                    description=(
                        f"**Scan results:**\n"
                        f"• Total entries scanned: {total_scanned}\n"
                        f"• Low-quality removed: {total_removed}\n"
                        f"• Clean entries kept: {total_scanned - total_removed}\n\n"
                        f"**Top removal reasons:**\n{reasons_str}\n\n"
                        f"**Status:** Cache has been cleaned. AutoPlay will now use updated heuristics."
                    ),
                    color=discord.Color.green(),
                )
            else:
                embed = discord.Embed(
                    title="✅ Cache is Clean",
                    description=(
                        f"**Scan results:**\n"
                        f"• Total entries scanned: {total_scanned}\n"
                        f"• Low-quality found: 0\n\n"
                        f"**Status:** No issues detected. Cache is already clean!"
                    ),
                    color=discord.Color.green(),
                )

            await ctx.send(embed=embed)

        except Exception as e:
            error_embed = discord.Embed(
                title="❌ Scan Failed",
                description=f"Failed to scan cache: {e}",
                color=discord.Color.red(),
            )
            await ctx.send(embed=error_embed)


async def setup(bot):
    await bot.add_cog(AutoPlaySettings(bot))
