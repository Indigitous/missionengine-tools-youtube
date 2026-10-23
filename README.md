# YouTube New-Video Tracker

Reads a list of YouTube channel URLs from a private Google Sheet, finds videos
published since the last successful run, and appends them to a local CSV.
Intended to run weekly via Windows Task Scheduler.

## Setup

1. Enable **YouTube Data API v3** and **Google Sheets API** in a Google Cloud project.
2. Create a YouTube API key, then set it:
   ```
   setx YOUTUBE_API_KEY "your_youtube_api_key"
   ```
3. Create a service account, download its JSON key as `service_account.json`
   (next to the script), and share your Google Sheet with the `client_email`
   inside it as **Viewer**.
4. Copy `config.example.json` to `config.json` and fill it in:
   - `sheet_id` - the long ID in your sheet's URL (between `/d/` and `/edit`)
   - `sheet_tab` - tab name holding the channel list
   - `url_column_header` - header of the column with channel URLs
   - `service_account_file` - path to the key file (relative paths are resolved
     next to the script; use forward slashes in JSON paths)
   - `new_channel_lookback_days` - how many days back to look for a channel that
     has never been scraped before (default 7). Use `"all"` to fetch every video
     from the beginning. Channels that were already scraped are not affected; to
     re-fetch one, delete its entry under `"channels"` in `state.json` (or delete
     `state.json` to redo everything). The older name `first_run_lookback_days`
     is still accepted.
5. Install dependencies and test:
   ```
   pip install -r requirements.txt
   python yt_tracker.py
   ```

## Scheduling (Windows)

```
schtasks /Create /TN "YouTubeTracker" /SC WEEKLY /D MON /ST 09:00 /TR "\"C:\Path\to\python.exe\" \"C:\Path\to\yt_tracker.py\""
```

In Task Scheduler, enable "Run task as soon as possible after a scheduled start
is missed" so a sleeping machine catches up.

## Output

- `new_videos.csv` - appended each run with only new videos
- `state.json` - when each channel (by channel ID) was last scraped; a channel
  missing from it counts as "never scraped" and gets the new-channel lookback
- `channel_cache.json` - resolved channel IDs (saves API quota)
- `yt_tracker.log` - run log

All of the above, plus `service_account.json` and your `config.json`, are
git-ignored. `config.example.json` is the committed template.

## Uploading new videos to another API (optional)

After each run, the videos found in that run can be sent to another API, one
`POST` per video, after logging in with the OAuth **password grant**. It is off
unless `upload.enabled` is `true` in `config.json`. Each video becomes an event
like this (the API replies `202 Accepted`):

```json
{"type": "resource", "url": "https://www.youtube.com/watch?v=...",
 "title": "Video title", "description": "New video from Channel", "tags": "youtube"}
```

1. Fill in the `upload` block of `config.json` (see `config.example.json`):
   - `api_url` / `token_url` - the events endpoint and the token endpoint
   - `include_grant_type` - `false` sends only `username` and `password` to the
     token endpoint (what the sample script does); `true` also sends
     `grant_type=password`, as the OAuth standard says
   - `scope`, `client_auth` - only needed if the API asks for them
   - `event` - the JSON sent for each video. Values are templates; these
     placeholders are filled in per video: `{video_url}`, `{video_title}`,
     `{video_description}`, `{video_id}`, `{channel_name}`, `{channel_url}`,
     `{published_at}`, `{fetched_at}`. Anything else is sent as written, so
     `"type": "resource"` is always `resource`. In `tags`, commas inside
     substituted values are replaced with spaces so they don't split a tag.
     Use `{{` and `}}` for literal braces.
   - `description_max_chars` - the `description` value is cut to this length
   - `timeout_seconds` - per-request timeout
2. Set the secrets as environment variables (they never go in a file):
   ```
   setx UPLOAD_API_USERNAME "your_username"
   setx UPLOAD_API_PASSWORD "your_password"
   ```
   `UPLOAD_API_CLIENT_ID` and `UPLOAD_API_CLIENT_SECRET` are optional; set them
   only if the API requires a client login. Open a new terminal afterwards.
3. `pip install -r requirements.txt` (this adds `requests`).

URLs must be `https://` (plain `http://` is only allowed for `localhost`).

**When something goes wrong**
- **The API can't take it right now** (server error, no network, 403, 404, 429,
  or a login that fails): the events that did not go through are kept in
  `pending_upload.json` and sent first on the next run.
- **The API refuses one event** (400, 409, 413 or 422 - bad data, or it already
  has it): that event is logged, saved in `rejected_uploads.json` with the
  reason, and skipped, so it can't block the others. If 5 events in a row are
  refused, the script stops and keeps them queued, because that points to wrong
  `event` settings rather than bad data.
- The CSV and `state.json` are always updated before the upload. A failed upload
  makes the script exit with code 1 so Task Scheduler shows it.
- Sending is one request per video, so a big backfill takes a while.
- A request can succeed even when its response is lost, so a video may rarely be
  sent twice. The API should treat `url` as unique.

## Design

The script follows the object-oriented design principles from *Head First
Object-Oriented Analysis & Design*: encapsulate what varies, program to an
interface, favor composition, one responsibility per class.

| Class | Responsibility |
| --- | --- |
| `Tracker` | Runs one scrape. Knows nothing about Google, CSV or JSON. |
| `ChannelSource` (`GoogleSheetChannelSource`) | Where channel URLs come from |
| `VideoPlatform` (`YouTubePlatform`, `CachedPlatform`) | Find channels, list videos; the cache wraps the platform |
| `LookupStrategy` (`ById`, `ByHandle`, `ByUsername`, `ByCustomUrl`) | One class per YouTube URL style |
| `VideoStore` (`CsvVideoStore`) | Where results are saved |
| `VideoPublisher` (`EventApiPublisher`, `RetryingPublisher`, `NullPublisher`) | Sending new videos to another API; the retrying wrapper queues failures |
| `AccessTokenProvider` (`PasswordGrantTokenProvider`) | Gets and caches OAuth tokens |
| `ScrapeHistory` | Which channels were scraped, and when |
| `Settings` | Contents of `config.json` |

To read channels from somewhere else, or save somewhere else, add one class that
implements the interface and swap it in `build_tracker()`. `Tracker` doesn't change.