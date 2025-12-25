# Core Idea

Quality over speed. Users have to wait for a certain amount of time for cold start. While the bot initializes

> "Apple Music's algorithm analyzes a vast array of data points, from individual listening habits to broader trends in music consumption. This includes the genres a user prefers, the artists they listen to most, how often they play certain songs, and even the time of day they're most active."

# Recommender
>
> As we are limited by the last.fm api. Its going to rely on the last.fm API to fetch the songs similar to that track. As last.fm is not really accurate in recommending its next song the bot is going to read the recommendation from the list for computed vibes using Librosa/EffecientAT/GeminiAPI. All to fetch the track data and know more, this is then going to get stored in a cache file.

> Its going to fetch a pool of around 100 songs, Checking the existing pool if it can find already enriched files or include the cache pool so it becomes 100 uningested songs & around ~50++ or ~200 or more enriched tracks. Its then going to check and filterout either neely played songs, or stale or artists. It should also know if its a collaboration and might be able to expand the novelty to include new artists and transition smoothly.

>

# fetch Im trying to build a apple music recommender but in discord. The limitation is Last.fm Provides Track.getsimilar  but it isnt accurate nor does track.similar genre, tag, etc. So Im thinking here for the discord recommender, as rythm has one. But I want to replicate it.

Limitations

1. Each session has to start "Fresh" as a user might bee listening with others, or alone, or it might be a differnet person altogether as its in a guild.
2. There is no way to get music metadata, except doing or analyzing ourselves. Do that via EffecientAT librosa and gemini . To get the track preview. we get it from deezer. but as deezer sometimes doesnt provide BPM or Gain, We cannot use that.
3. Youtube mappings are a pain and must be solved as last.fm and deezer are quite picky
