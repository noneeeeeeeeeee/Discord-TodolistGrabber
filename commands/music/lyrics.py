import discord
from discord.ext import commands
import os
import aiohttp
from dotenv import load_dotenv
import urllib.parse
import json
from modules.readversion import read_current_version

# Load environment variables
load_dotenv()

MUSIXMATCH_API_KEY = os.getenv("MUSIXMATCH_API_KEY")

class Lyrics(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.current_lyrics_message = None 
        self.current_song = None 

    @commands.command(name="ly", aliases=["lyrics"])
    async def display_lyrics(self, ctx):
        """Displays lyrics for the currently playing song."""
        music_player = self.bot.get_cog("MusicPlayer")
        if not music_player or not music_player.now_playing.get(ctx.guild.id):
            await ctx.send(":x: No song is currently playing.", delete_after=10)
            return
        
        song_url, song_title, song_duration = music_player.now_playing[ctx.guild.id]
        
        if self.current_song == song_title:
            await ctx.send(":x: Lyrics are already being displayed for this song.", delete_after=10)
            return
        
        self.current_song = song_title
        
        # Create the initial embed with searching message
        embed = discord.Embed(
            title=song_title,
            description="",
            color=discord.Color.orange()
        )
        embed.set_author(name="Searching for lyrics...")
        embed.set_footer(text=f"Bot Version: {read_current_version()}")
        
        # Send the initial message
        self.current_lyrics_message = await ctx.send(embed=embed)

        # Try fetching lyrics from Musixmatch
        musixmatch_lyrics = await self.get_lyrics_musixmatch(song_title)
        if musixmatch_lyrics:
            await self.update_lyrics_message(ctx, musixmatch_lyrics)
            return
        
        # If Musixmatch fails, try fetching lyrics from LRC
        lrc_lyrics = await self.get_lyrics_lrc(song_title)
        if lrc_lyrics:
            await self.update_lyrics_message(ctx, lrc_lyrics)
            return
        
        # If both fail, edit the embed to show failure
        embed.set_author(name="Lyrics not found")
        await self.current_lyrics_message.edit(embed=embed)
        
        # Reset the state after failure
        self.reset_lyrics_state()

    async def get_lyrics_musixmatch(self, song_title):
        """Fetches lyrics from Musixmatch using the track.subtitle.get endpoint."""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.musixmatch.com/ws/1.1/track.search?q_track={song_title}&apikey={MUSIXMATCH_API_KEY}&f_has_lyrics=1"
                async with session.get(url) as response:
                    print(f"Musixmatch raw response: {await response.text()}")  # Debugging
                    if response.status == 200 and response.headers['Content-Type'].startswith('application/json'):
                        data = await response.json()
                        if data['message']['header']['status_code'] == 200:
                            lyrics_body = data['message']['body']['track_list']
                            if lyrics_body:
                                # Get the first track's lyrics_id
                                track = lyrics_body[0]['track']
                                lyrics_id = track['lyrics_id']
                                title = track['track_name']  # Get the track title

                                # Now fetch the actual lyrics using the lyrics_id
                                lyrics = await self.get_lyrics_by_id(lyrics_id)
                                return lyrics, title  # Return the lyrics and title
                        else:
                            print(f"Error in response: {data['message']['body']['error_message']}")
                    else:
                        print(f"Unexpected content type: {response.headers['Content-Type']}")
                        print(f"Response status: {response.status}, content: {await response.text()}")
        except Exception as e:
            print(f"Musixmatch API error: {e}")
        return None

    async def get_lyrics_by_id(self, lyrics_id):
        """Fetches lyrics using the lyrics_id from Musixmatch."""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.musixmatch.com/ws/1.1/lyrics.get?lyrics_id={lyrics_id}&apikey={MUSIXMATCH_API_KEY}"
                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data['message']['header']['status_code'] == 200:
                            return data['message']['body']['lyrics']['lyrics_body']  # Return the lyrics body
                        else:
                            print(f"Error fetching lyrics: {data['message']['body']['error_message']}")
                    else:
                        print(f"Failed to fetch lyrics. Status: {response.status}")
        except Exception as e:
            print(f"Error in get_lyrics_by_id: {e}")

        return None

    async def get_lyrics_lrc(self, song_title):
        """Fetches LRC lyrics from your existing implementation."""
        try:
            async with aiohttp.ClientSession() as session:
                search_term = f"{song_title}"
                encoded_search_term = urllib.parse.quote(search_term)
                print(f"Searching for lyrics: {search_term}")
                print(f"Encoded search term: {encoded_search_term}")
                url = f"https://lrclib.net/api/search?q={encoded_search_term}"

                async with session.get(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data:
                            first_result = data[0]
                            if 'plainLyrics' in first_result and first_result['plainLyrics']:
                               return first_result['plainLyrics'].replace(' ', '')  # Ensure no spaces between characters
                            else:
                                print("No lyrics found in the first LRCLIB search result.")
                        else:
                            print("No results found in LRCLIB search.")
                    elif response.status == 404:
                        print("Track not found in LRCLIB.")
                    else:
                        print(f"Unexpected response status from LRCLIB: {response.status}")
                        print(await response.text())
        except Exception as e:
            print(f"LRCLIB API error: {e}")

        return None

    async def update_lyrics_message(self, ctx, lyrics_info):
        """Update the current lyrics message with the fetched lyrics."""
        lyrics, title = lyrics_info  # Unpack the lyrics and title
        full_lyrics = lyrics.replace("\\n", "\n")  # Properly handle new line characters

        embed = discord.Embed(
            title=f"Lyrics for {title}",
            description=full_lyrics,
            color=discord.Color.blue()
        )

        if self.current_lyrics_message:
            await self.current_lyrics_message.edit(embed=embed)
        else:
            self.current_lyrics_message = await ctx.send(embed=embed)

    def reset_lyrics_state(self):
        """Resets the lyrics display state."""
        self.current_song = None
        self.current_lyrics_message = None

async def setup(bot):
    await bot.add_cog(Lyrics(bot))
