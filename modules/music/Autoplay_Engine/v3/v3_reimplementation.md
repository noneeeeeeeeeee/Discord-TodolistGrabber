# Core Idea

> Quality over speed. Users have to wait until it finishes building up a database for a cold start while the bot initializes
> It will show high level progress updates on what its doing if its taking a while. For example: Analyzing Context, Analyzing  candidates (50/1000), Finalizing pick.
This high level progress update is shown on discord and updated per 10s after it takes longer than 30s.

> "Apple Music's algorithm analyzes a vast array of data points, from individual listening habits to broader trends in music consumption. This includes the genres a user prefers, the artists they listen to most, how often they play certain songs, and even the time of day they're most active."

# Limitations

1. Each session has to start "Fresh" as a user might bee listening with others, or alone, or it might be a differnet person altogether as its in a guild.
2. There is no way to get music metadata, except doing or analyzing ourselves. Do that via EffecientAT librosa and gemini . To get the track preview. we get it from deezer. but as deezer sometimes doesnt provide BPM or Gain, We cannot use that.
3. Youtube mappings are a pain and must be solved as last.fm and deezer are quite picky

# Recommender
>
> This is the brains of the operation. Main panel and where the final step is and where the recommender will output.
> All the recommender does is take in all the data points and the database points compare it also with the context (history). It also should be able to request more if it doesnt fit the threshold or if it needs more data.
> This will manage apple music style where its like a slow burn and doesnt transition fast.
> last system I used a Cold, Warmm, hot pool
where cold is theres not enough context (1-... songs) I need to guess and very unstable
Warm is where it gets stable and should stabalize itself
Hot means its starting to diversify and should keep that cooking. If the transition was successfull its going ot go back to warm where its in a stable state.

# Daydreamer
>
> This daydreamer only runs int he background if there are no active sessions. Talking with Session Manager. Its goin to put itself in priority 3 and start exploring. Once it sees the cache reach 5000 songs its going to change the strategy.
A. If its leess than 5000 its going to explore. Once a month its going to fetch a bunch of songs that are new releases and double checks for duplicates. If thats done then its just going to find other songs on a per genre basis rolling. Its going to search and fetch 100 songs per 30 minutes if theres no sessions.
B. if its more than 5000 its just going to run slower meaning its going to fetch 50 songs per hour if theres no session to find it.
> New song logic: The daydreamre is going to fetch the new songs from deezer top 500 or max. Whatever it is. If it corss checks but then uhoh, it exceeds 100 or 50 songs in the batch. Its going to signal the next batch to keep checking until the new releases is empty then its goingo to keep rolling on.
> Song fetching logic: Per batch strt its going to keep track on what genre or mood its on and keep exploring like changing shifts. Its going to read the lsat shift manager notes and keep track. If it cant find anymore its going to try again for like 3 tries trying different terms etc. And move to the next tag, genre, or mood depending on what it needs from the cache. So the distribution of each GENRE not Sub Genre. Should be about equal. As an example: lets say 50 songs in pop 100 songs in rock, and 75 in edm. So it just keeps everything in check.
> What if it cant find after x retries? and even after genre switching. Its then going to just leave whatever it is and stop telling a note that it failed and for the next shift to try again or change strategy.
> what if its in a middle of a batch then theres a session active? well its going to stop that and end that run giving notes to the next run. As the daydreamer requests is going to get postponed giving the higher prios more.
> What if a session is active when the next batch rolls? its going to just not run that batch and wait another 30 or 1 hour depending on how much is in the cahce.
> What if the bot suddenly turns off? its going to continue where it left off. Either A. continue the batch or B. Continue waiting the remainng time.

# Mappings
>
> Mappings from Last.fm Deezer and Youtube.
> If the song is played via user then it finishes and there is no more songs its going to store that > Use Gemini 2.5-flash-lite to parse the youtube + channel > into readable format > request deezer api (this deezer api is like a searchbar in the deezer app and leeaves more leeway for incorrect search terms) > then its going to get the best match from the gemini and choose that. on a threshold of around 65% with 0.5 +- error correction. If all fails its going to retry again this time using google grounding to search up the correct format (Example search format: {Song Title} {Artist}) (Wrong format: {Song Title} - {Artist}, or {Song Title} {Artist} English, or {Song Title} English version...). If that fails also use {Song Title} And search the best one on the search list.
> If the song is gotten from last.fm (this is building up a pool) > Bulk search via deezer and map it to deezer > the failed results will get bulk requested to gemini api, request is 50 max per batch using grounding. And retried on deezer.

> Deezer will always have 100% of the music so if it fails its probably a podcast or nonmusic and it should disable it. It will then show an error that it couldnt find the the mapping and a report option. If that report option is pressed (One time only, expiry of 30s) then its going to send to the webhook in .env (not setup yet). On webhook_url. where all the reports go there. If its not set then there iwll be no button
> Additionally. If its gotten form last.fm its probably a bulk fetch where the recommender is trying to get more info on the songs, thus it shouldnt try to map to youtube yet.

> Once the recommender spits out a song or spits out the buffer (5 songs). This is now going to search for that in youtube and then put it in queue and also store it in the respective mapping.
> MAPPINGS IS WHERE PARSING ALSO EXIST. IT IS EVERYTHING TODO WITH URLS, NAMES, ID's, IDENTIFIERS, BUT NOT SONG METADATA SUCH AS: Dancibility, Acoustic, BPM, etc.
Summary: This mapping module is mapping from Deezer <> Youtube <> Last.fm. IT DOES NOT MANAGE THE MAPPING CACHE

# Cahce Manager
>
> This is where all the cache and temporary session information is stored deleted and managed. It manages cache from Mappings (parsed title and all that), temporary sessions (in this case, active sessions where it stores data for the context analyzer to analyze), song metadata storage.
> If the json exceeds 500 entries on all its going to create another one making it sharded. So this is like a supply manager where it manages and fetches all things storage related and gives it in neat format.
> lets say mappings_a.json has exceeded 500 entries. Its going to then create mappings_b.json. What if the letter reaches z? then its going to go to mappings_aa.json, mappings_ab.json and so on. Fetching it also depending on what the other modules want to fetch. Or search for, its going to return and should be very robust.
> THis is like a person with a forklift in a warehouse retreiving items.

# Context Analyzer
>
> This analyzes the past played songs and the duration, where the user skipped. What they dislike and buids a profile of it. It reads all the songs currently in session. And weighs the older played song lower than the newer one. And gives like a "Summary" of sorts to the recommender from the song metadata.
> It also analyzes why the user skips multiple times lets say (5 consequtive) or 3 or skips only 1 time and finds a pattern for the recommender to take account.
> As 1 skip might be the user isnt in to it right now not that song or it can even mean I dont like that genre
> 2 skips might lean more to not that artist or mood right now
> 3 is where they might not like that genre.
> This context analyzer also takes account per user. Where it will read from the session data if the user(s) like the song or not or how many votes out of the people in the VC. If they vote "More Like this" 2 ppl voted out of 10 it will be different than a solo in the voice channel.
> Less like this will be removed, as it will take account via consequtive skips.

adding Vibe Momentum:

Session Evolution: Don't just average the last 5 songs. Use a decaying weight where the most recent song represents 50% of the "vibe," and the previous four fill the remaining 50%. This allows the bot to pivot faster when the group's taste shifts.

Repetition "Soft-Bans": Your current penalty uses exponential decay. Improve this by adding an Artist Cooldown in the ContextualRecommender to prevent the bot from playing the same artist too frequently, which is a common complaint in autoplay systems.

# Song Analyzer
>
> This analyzes either in bulk or not, any songs requesed by the recommender. It can be in bulk or not, and manages. This song analyzer then can trigger the recommender if its done analyzing the current request to say that its "done".
> This also manages the priority from 1. Highest to 3. Daydreaming mode.
> The queue file is also going to be created to keep track and make sure to continue where it left off if the bot suddently shuts off.
>
> 1. should be active on session where it needs it NOW!!!
> 2. should be the buffer manager request where it starts filling up.
> If a song doesnt have a deezer metadata but has youtube or last.fm its going to talk with the mappipng to request that or complete its profile. Since the song analyzer needs the 30s preview deezer provides to analyze.
> it uses 3 analysis programs.
>
1. Gemini: Cultural context
2. EffecientAT: More in depth analysis located in Dependency manager. this should be low level.
3. Librosa: Aids in EffecientAT where it cannot fill in, like the high level
Either number 2.3 is fliped on high or low level. But both should analyze either high or low level metadata. with gemini filling the rest like BPM, tags, genre, Any culture.

**V3 Implementation - Worker Pool Architecture:**

- EfficientAT and Librosa analysis uses a **ThreadPoolExecutor** with configurable workers (default: 3 workers)
- Worker count is configurable via `ANALYZER_CONFIG.analysis_worker_count` in constants.py
- This allows parallel CPU-bound audio analysis without blocking the event loop

**V3 Implementation - Bulk Gemini Processing:**

- Gemini API calls are **batched** to avoid rate limiting and API spam
- Songs are queued via `_queue_for_gemini_batch()` and processed together
- Batch settings: `gemini_batch_size=10`, `gemini_batch_timeout=5.0s`
- Only IMMEDIATE priority requests get inline Gemini calls; all others go through the batch queue
- A background worker (`_gemini_batch_worker`) processes batches when size threshold or timeout is reached

> Now this song analyzer also checks if theres also any songs analyzed already inthe cahce and skips filters out any if its already done saving time. What the other modules need to know is the data so this wil return the data also from the stored and new.

Harmonic Mixing (The Pro DJ Touch): Use librosa.feature.chroma_cqt to detect the Musical Key and Mode (Major/Minor). You already have logic for key compatibility in contextual_recommender.py. Prioritize "Camelot Wheel" adjacent keys (e.g., C Major to G Major) to make the transitions between songs feel seamless.

Loudness Normalization (Gain): Use the loudness calculated in AnalysisResult to set a target gain. This prevents "volume jumps" where one song is significantly louder than the next, a critical part of a premium listening experience.

# Gemini Manager

Now this is important. All the parsing and song analysis go here for the manager to manage, This is to ensure gemini works 100%. No API rate limit. And manages the rotation of the multiple API keys. If the user places more in the .env

It also makes or mocks a "Bulk Request" as bulk reuqest is only for enterprise. And im on free. it will try to save API requests by bunching them up together from the other modules request. But as it doesnt know how much the other modules will request the other modules have to state how many entries they are requesting then this manager is going to manage it all and return the finished and compiled data. It will also auto retry if one entry isnt answered or misses and keeps doing so until all is done. It manages grounding or searching on google and manages if it needs a higher or smarter model i.e. Gemini 2.5-Flash rather than Gemini 2.5-Flash-lite

It also manages rate limites to ensure it doesnt get rate limited and from what the other modules need to know is the return data also.

# Novelty Manager
>
> This controls the novelty of the song meaning its going to "Nudge" the recommmender. Both of these modules should be interconnected or like "Talking to each other" form the data given. Like a person trapped in an escape room that is trying to find the escape, this is trying to find the right genre and not stray away and also not make the person or persons in the vc bored of listening.

# Buffer Manager
>
> This manages the 5 buffer in the songs and also talks with the novelty manager and recommender where what it should put in. And what songs it should put in next, and also notify and delete all the buffer if the user reuqests a song after a while.
Exmaple: user Adds song A > Song ends > Sees the queue finished > Recommends the next song > Plays Song B > buffer starts filling > Song B ends > Song C starts playing from buffer > user adds song D > user skips song C > buffer should then clear and notify to reanalyze the context on why it skips

**V3 Implementation - Apple Music-Style Slot Types:**
> Each buffer slot has a type: **SAFE** or **EXPLORATORY**
>
> - SAFE slots: Songs similar to established preferences (familiar picks)
> - EXPLORATORY slots: Songs that "test the waters" with new genres/artists
>
> Slot composition changes based on session phase:
>
> - `early` (0-5 songs): 4 SAFE, 1 EXPLORATORY (80% safe - build trust)
> - `establishing` (5-15 songs): 3 SAFE, 2 EXPLORATORY (60% safe - start exploring)
> - `confident` (15+ songs): 2 SAFE, 3 EXPLORATORY (40% safe - actively diversify)
>
> The buffer tracks exploratory success rate and adjusts future allocations accordingly.

# Vector Search Index (formerly Content Similarity Engine)
>
> **V3 Implementation: `vector_search_index.py`**
> Now this needs a bunch of data and assists as a sous chef also. its like the context analyzer except it analyzes all the songs int he metadata gets the vector or math init and gatehr the data in abulk way to help.
> But this only can kick in if there is 500+ songs in the cache, song metadata. So it can calculate all the 9D vectors and such.
>
> Key Classes:
>
> - `VectorSearcher`: Main class for audio similarity search
> - `VectorIndex`: Efficient cosine similarity search with 3-layer weighting (EfficientAT=0.5, Librosa=0.3, Gemini=0.2)
> - `SimilarityResult`: Result container with song_id, similarity_score, match_reasons

# Collaborative Recommender (True Collaborative Filtering)
>
> **V3 Implementation: `collaborative_recommender.py`** (NEW - separated from vector search)
> AS this is per guild now:>
> Key Classes (V3 Implementation):
>
> - `TransitionMatrix`: Tracks "what plays after what" with success/skip counts
> - `UserBehaviorProfile`: Per-user preferences across sessions (genre/artist affinity, skip patterns)
> - `GroupConsensus`: Aggregates preferences when multiple users are in VC
> - `CollaborativeRecommender`: Main class that combines transition scoring + behavioral boosts
Since you have rebased the codebase to a "More Like This" focus and centralized feedback into a Context Analyzer, you’ve essentially moved toward a Reinforcement Learning (RL) style of recommendation.

In a Discord environment where "User A" and "User B" might have different tastes, your Collaborative Filtering (CF) should act as the Global Knowledge Base, while the Context Analyzer acts as the Local Vibe Controller.

Here is how you should structure the CF to handle the "Group/Session" dynamic without getting confused by multiple users.

1. The Data Model: "Session-as-Document"
Instead of tracking individual users, treat each Discord session as a single "Document" of music taste.

The Input: The list of songs that were actually played (not skipped) in a guild's session.

The Collaborative Signal: If "Session 1" in Guild A played Song X and then clicked "More Like This" to get to Song Y, you have a strong behavioral link.

1. Implementation: The "Transition Matrix"
Inside your collaborative_filtering.py, you shouldn't just look for acoustic similarity. You should implement a Transition Matrix that records the "More Like This" path.

2. Handling the "Infinite Users" Problem
Since you don't know which of the 10 people in the VC clicked "More Like This," you use a Group Consensus logic:

Vote Aggregation: If your bot has buttons, track how many different users clicked "More Like This" for the same song.

The "Vibe Vector" Shift: Instead of replacing the current search query with the new song, the Context Analyzer should nudge the current session vector.

Example: If the group is listening to Lo-Fi, and someone clicks "More Like This" on a Jazz track, don't jump 100% to Jazz. Move the session vector 20% toward Jazz. If more people interact, move it further.

Hybrid Recommendation Scoring
Currently, your CollaborativeFilterer is performing content-based matching using EfficientAT and Librosa vectors. To make it truly collaborative in a Discord group setting, you should implement a Transition Probability Score.

Behavioral Boost: Instead of just finding songs that sound like the seed, cross-reference your v3_training_data_*.jsonl logs. If "Song B" frequently follows "Song A" across multiple guilds without being skipped, boost its rank even if its acoustic similarity is lower.

The "Group Consensus" Factor: Since sessions have multiple users, weigh "More Like This" clicks by the number of unique users interacting. If 3 people click the button for the same song, it’s a much stronger signal than one person spamming it.

# recommemnder system (Overview)

This isnt a file this is just a rundown on the recommender where it consists of around 4 modules interconnected to make it very close or likewise to aple music recommendation

**V3 Module Structure (Updated):**

- `vector_search_index.py` - Audio content similarity (EfficientAT/Librosa vectors)
- `collaborative_recommender.py` - User behavior tracking (transitions, preferences, group consensus)
- `context_tracker.py` - Session state and mood detection
- `novelty_controller.py` - Exploration vs exploitation balance
- `buffer_manager.py` - 5-song lookahead with SAFE/EXPLORATORY slots
- `song_analyzer.py` - Worker pool for audio analysis + bulk Gemini

The Novelty manager, Context analyzer, Recommender, Vector Search, Collaborative Recommender, and buffer manager all should be connected like talking to each otehr
> like the recommender is the head chef and the context analyzer is the waiter and also giving feedback, the buffer manager is like the sous chef waiting for orders, and the novelty manager is also the sous chef but right beside the head chef meaning they are helping each other out. The vector search and collaborative recommender are like specialized chefs - one handles ingredients (audio similarity) while the other knows the customers' preferences (behavior patterns).
> Additionally, there will be a daydreamer, this is what later will power the new songs fetcher and runs in the background. The recommender will stop trying to call last.fm for new songs and just use the cache or built up song metadata once it reaches 5000 songs, for now its going to continue with it while checking the cahce and removing any thats duplicate from the cahce.
> Each module not every module. Just the one that stores cache should have a Version tag below. if there is a major update to it or a change in logic or a better algorithm its going to reftech it when theres no sessions and idle. Meaning it will skip the daydreamer until its done updating all the cahe. IF theres a sesssion then thats active its going to pause that meaning removing all the queue and such and priotizing the session. If the session then needs a song that it turs out is outdated its goin to then request for an update on that on the fly.

# Session Manager
>
> This manages how many active sessions can be allowed in the recommender as too much will make it overwhelmed. Like a restoraunt at full capacity but the front desk keeps accepting new customers even though theres no more seats making it cahos.
> For now once a channel reuqests a song and the recommender kicks in (if they have it enabled) its going to go through here and asking like "Hey is there a seat available?" and if it replies with Yes its going to then have that table reserved for the rest of the active session.
Until either:
A. The bot leaves.
B. The user requests more than 5 songs instantly. Example:
  > Meaning song 1-10 consequtivley is theirs
  > this doesnt mean Song 1-8 is theirs then 9 recommendations then 10 the user requests again. This will not trigger a session end.
  > So it has to be that the user requests 5 (configurable) songs it can be that they request song A wait for it to end then song B song C and so on. Or instantly. -1 to disable or set to 0.
C. User disables it in the settings

> If lets say it retunrs no. Then the bot will just let the queue finish and tell that There are no more songs in the queue.
> It will keep requesting every queue end and as such each active session will have the song stored in the cahce. (This will be delted once the sesion ends like the bot leaves.) . But its going to have it stored and all the user or interaction data for later if they actually trigger the reocmmender. UNLESS: The recommender is disabled in settings it can be disabled in the middle of the session where it will then delet the cahce or disabled at the start where it wont start.
> What happens if the user enables it in the middle? its going to tell that its going to be active in the next session instead of this one.

URLS:
<https://github.com/fschmid56/EfficientAT/releases/download/v0.0.1/mn10_as_mAP_471.pt>
<https://librosa.org/doc/latest/install.html>
<https://ai.google.dev/gemini-api/docs>
<https://www.last.fm/api>

DEEZER API:
<https://developers.deezer.com/api>
THIS ONE IS WITHOUT AUTH:
<https://publicapis.io/deezer-api>
IF WEBSITE CANT BE READ: THIS WILL CONVERT IT TO A README FORMAT.
<https://r.jina.ai/{url}>

## Improved Optimizations

1. Dynamic "Look-Ahead" Buffering
Instead of a static cold start that finishes once the bot is "ready," implement a continuous look-ahead buffer.

Predictive Analysis: While the current song is playing, the AutoplayEngineV3 should use the idle CPU time to analyze the next 5–10 most likely candidates from the current "More Like This" pool.

Pre-emptive Fetching: If the buffer falls below a certain threshold (e.g., fewer than 3 analyzed songs ready), trigger an emergency enrichment pass to ensure the user never sees a "loading" state after the initial cold start.

1. Session Persistence (The "Warm" Start)
The current limitation is that each session starts fresh. To improve the buffer:

Cross-Session Cache: Allow the bot to retain the acoustic analysis (BPM, vectors, embeddings) of songs it has already seen in previous sessions or other guilds, while resetting the behavioral context.

Result: This drastically reduces the "Cold Start" time for popular songs that have already been "buffered" by the bot globally.

1. Buffer Prioritization Logic
Modify how the buffer chooses which songs to analyze first:

Seed-Centricity: Prioritize candidates that are closest in the vector space to the current queue. Analyzing 1,000 random songs is less efficient than analyzing 100 songs that actually fit the current group's vibe.

Failure Fallbacks: If the buffer encounters a track where the Deezer preview fails, have it immediately move to the next candidate rather than retrying, to keep the progress bar moving for the user.

1. Interaction-Driven Buffering
Since you are moving to a "More Like This" interaction model, use that as a priority signal:

On-Click Priority: If a user clicks "More Like This," that specific track and its immediate acoustic neighbors should jump to the front of the analysis queue.

Contextual Pruning: If the ContextTracker detects a "Rising Boredom" phase (high skips), the buffer should clear its current "Stable" candidates and start buffering higher-novelty tracks to pivot the session.
Progress Observability (The "Wait UX")
Current State: You plan to show progress updates every 10s after the first 30s of waiting.

Improvement: Use the Discord Embed to show a more detailed "System Health" or "Analysis Pipeline" view.

Example: Instead of just "50/1000", show:

[████░░░░░░] 40% - Analyzing Harmonic Structures

[██████░░░░] 60% - Generating Neural Embeddings (EfficientAT)
