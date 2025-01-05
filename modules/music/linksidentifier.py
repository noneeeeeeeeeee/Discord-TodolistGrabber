import re


class LinksIdentifier:
    PATTERNS = {
        "YouTube": {
            "playlist": r"https://www\.youtube\.com/.*[?&]list=([^&]+)",
            "track": r"https://www\.youtube\.com/watch\?v=([^&]+).*",
        },
        "Spotify": {
            "track": r"https://open\.spotify\.com/track/([^?]+)",
            "playlist": r"https://open\.spotify\.com/playlist/([^?]+)",
            "album": r"https://open\.spotify\.com/album/([^?]+)",
            "artist": r"https://open\.spotify\.com/artist/([^?]+)",
        },
        "AppleMusic": {
            "station": r"https://music\.apple\.com/.*/station/([^?]+)",
            "playlist": r"https://music\.apple\.com/.*/playlist/([^?]+)",
            "album": r"https://music\.apple\.com/.*/album/([^?]+)",
            "artist": r"https://music\.apple\.com/.*/artist/([^?]+)",
        },
    }

    @staticmethod
    def identify_link(input_link: str):
        # Iterate through patterns to find a match
        for provider, types in LinksIdentifier.PATTERNS.items():
            for link_type, pattern in types.items():
                match = re.search(pattern, input_link)
                if match:
                    # If a match is found, return parsed data in JSON format
                    return {
                        "musicProvider": provider,
                        "type": link_type,
                        "id": match.group(1),
                    }
        # Return None if no pattern matches the input link
        return None
