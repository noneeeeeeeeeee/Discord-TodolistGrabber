import os
import yt_dlp as youtube_dl
import asyncio
from modules.enviromentfilegenerator import check_and_load_env_file
import googleapiclient.discovery
import urllib.parse

class YouTubeFetcher:
    def __init__(self):
        check_and_load_env_file()
        self.youtube_api_key = os.getenv("YOUTUBE_API_KEY")
        if not self.youtube_api_key:
            raise ValueError("YouTube API key is missing. Ensure it's set in the environment variables.")
        
        # Initialize the YouTube API client without a custom build_request function
        self.youtube_service = googleapiclient.discovery.build(
            "youtube", "v3", developerKey=self.youtube_api_key
        )

        self.youtube_dl_options = {
            'format': 'bestaudio/best',
            'quiet': True,
            'nocheckcertificate': True,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }]
        }

    async def handle_playlist(self, playlist_url, music_queue, guild_id, max_duration, max_results=50):
        """Handles playlist processing by fetching items and adding them to the music queue."""
        # Parse the playlist URL to extract the playlist ID
        parsed_url = urllib.parse.urlparse(playlist_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        playlist_id = query_params.get("list", [None])[0]
        if not playlist_id:
            raise ValueError("Invalid playlist URL. Could not extract the playlist ID.")
        
        # Fetch playlist items
        try:
            playlist_items = await self.fetch_playlist_items(playlist_id, max_results)
            added_videos = 0
            skipped_videos = 0
            tasks = [
                self.process_video_entry(item, music_queue, guild_id, max_duration)
                for item in playlist_items
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for result in results:
                if isinstance(result, Exception):
                    skipped_videos += 1
                else:
                    added_videos += 1

            return added_videos, skipped_videos

        except Exception as e:
            print(f"Error retrieving playlist: {e}")
            raise e

    async def fetch_playlist_items(self, playlist_id, max_results=50):
        """Fetches videos from a YouTube playlist using the YouTube Data API v3."""
        videos = []
        next_page_token = None

        while len(videos) < max_results:
            request = self.youtube_service.playlistItems().list(
                part="snippet",
                maxResults=min(max_results - len(videos), 50),
                playlistId=playlist_id,
                pageToken=next_page_token
            )
            response = request.execute()

            for item in response["items"]:
                video_id = item["snippet"]["resourceId"]["videoId"]
                title = item["snippet"]["title"]
                videos.append({"id": video_id, "title": title})

            next_page_token = response.get("nextPageToken")
            if not next_page_token:
                break

        return videos

    async def process_video_entry(self, video, music_queue, guild_id, track_max_duration, author):
        """Processes a single video entry and adds it to the music queue if it meets the criteria."""
        try:
            video_id = video["id"]
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            info = await self.extract_info(video_url)

            if info is None or 'formats' not in info:
                print(f"Could not extract info for video: {video_url}")
                return

            audio_url = next(
                (f['url'] for f in info['formats'] if f.get('acodec') and f['acodec'] != 'none'),
                None
            )

            title = info.get('title', 'Unknown Title')
            duration = info.get('duration', 0)

            if audio_url and duration <= track_max_duration:
                music_queue.setdefault(guild_id, []).append((author, audio_url, video_url, title, duration))
            else:
                print(f"Skipped video: {title}, no audio URL found or duration exceeds limit.")

        except Exception as e:
            print(f"Error adding video to queue: {e}")

    async def extract_info(self, url):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.extract_info_sync, url)

    def extract_info_sync(self, url):
        with youtube_dl.YoutubeDL(self.youtube_dl_options) as ydl:
            return ydl.extract_info(url, download=False)

    async def search_youtube(self, query, top_n=1):
        try:
            response = await asyncio.to_thread(self.youtube_search_handler, query, top_n)

            search_results = []
            for item in response["items"]:
                video_id = item["id"]["videoId"]
                video_title = item["snippet"]["title"]
                video_url = f"https://www.youtube.com/watch?v={video_id}"
                search_results.append((video_url, video_title))

            return search_results

        except Exception as e:
            print(f"Error: {e}")
            return []

    def youtube_search_handler(self, query, top_n):
        request = self.youtube_service.search().list(
            q=query,
            part="snippet",
            maxResults=top_n,
            type="video",
            videoCategoryId="10",
            videoEmbeddable="true"
        )
        return request.execute()