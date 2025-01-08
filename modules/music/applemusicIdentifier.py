import re


class AppleMusicIdentifier:
    def __init__(self):
        self.am_patterns = {
            "track": r"https://music\.apple\.com/.*/([^/]+)/([^/]+)/([^?]+)",
            "album": r"https://music\.apple\.com/.*/album/([^/]+)/([^?]+)",
            "playlist": r"https://music\.apple\.com/.*/playlist/([^/]+)/([^?]+)",
        }

    def decode_url_title(self, encoded_title: str) -> str:
        """Convert URL-encoded title to human readable format."""
        # Replace hyphens and underscores with spaces
        decoded = encoded_title.replace("-", " ").replace("_", " ")
        # Remove any special URL encoding
        decoded = re.sub(r"%[0-9A-Fa-f]{2}", " ", decoded)
        return decoded.strip()

    def convert_to_search_query(self, url: str) -> str:
        """Convert Apple Music URL to a search-friendly format."""
        for media_type, pattern in self.am_patterns.items():
            match = re.match(pattern, url)
            if match:
                if media_type == "track":
                    # For tracks, use song name
                    title = self.decode_url_title(match.group(2))
                    return title
                elif media_type == "album":
                    # For albums, use album name
                    album_name = self.decode_url_title(match.group(1))
                    return f"album {album_name}"
                elif media_type == "playlist":
                    # For playlists, use playlist name
                    playlist_name = self.decode_url_title(match.group(1))
                    return f"playlist {playlist_name}"

        return None

    def get_search_terms(self, url: str) -> str:
        """Get search terms from Apple Music URL."""
        return self.convert_to_search_query(url)

    def convert_to_youtube_query(self, url: str) -> str:
        """Convert Apple Music URL to a YouTube Music search query."""
        return self.convert_to_search_query(url)
