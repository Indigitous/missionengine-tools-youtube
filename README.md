# YouTube New-Video Tracker

Reads a list of YouTube channel URLs from a private Google Sheet, finds videos
published since the last successful run, and appends them to a local CSV (and can
upload them to MissionEngine). Before scraping, it can add new YouTube links from
Airtable's Content Sources table to the Sheet.
Intended to run weekly via Windows Task Scheduler.

New here? Start with the one-page visual guide:
https://indigitous.github.io/missionengine-tools-youtube/ (source: `docs/index.html`).

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

## Adding new channels from Airtable (Content Sources)

The Google Sheet is the only list that gets scraped. If `config.json` has an
`airtable` block, each run does two steps:

1. **Airtable to Sheet.** Read the Content Sources table, keep only links to
   `youtube.com` or `youtu.be`, and add any that are missing from the Sheet as new
   rows at the bottom. Existing rows are never changed or removed. A link counts as
   already there even if it is written a bit differently (`http`/`https`, `www.`,
   capitals, a trailing `/`).
2. **Scrape the Sheet.** Check every channel in the Sheet for new videos, as
   before. Links added in step 1 are new channels, so they get
   `new_channel_lookback_days` of videos in this same run.

If step 1 fails (Airtable down, missing token, no write access), the error is
logged and step 2 still runs.

Each new row gets:
- `channel_url` - the link
- `channel_name` - the handle, username or ID taken from the link
- `attribution` - the Airtable field named in `attribution_field`
  (default `Short Description`), if it has a value
- `is_processed` - `0`

Other columns are left blank.

**Setup**

1. Create an Airtable personal access token at https://airtable.com/create/tokens
   with the `data.records:read` scope and access to the base, then set it:
   ```
   setx AIRTABLE_TOKEN "your_token"
   ```
2. Share the Google Sheet with the service account's `client_email` as **Editor**
   (Viewer is only enough for reading).
3. Fill in the `airtable` block of `config.json`:
   - `base_id` - the `app...` part of the base's URL
   - `table` - the table name (`Content Sources`) or its `tbl...` ID
   - `url_field` - the field holding the link (a URL, text or lookup field)
   - `attribution_field` - the field copied into `attribution` (`""` for none)
   - `view` - optional; only read records shown in this view
   - `enabled` - optional; `false` turns step 1 off without deleting the block
4. Check it:
   ```
   python yt_tracker.py --check-airtable           # list the YouTube links in Airtable
   python yt_tracker.py --copy-to-sheet --dry-run  # list what would be added to the Sheet
   python yt_tracker.py --copy-to-sheet            # do step 1 only, no scraping
   ```

**What each kind of link in the Sheet does**
- **Channel** (`/@handle`, `/channel/...`, `/user/...`, `/c/...`, or old-style
  `youtube.com/NAME`): new uploads from that channel. For `youtube.com/NAME` the
  handle is tried first, then the old username, then a search (100 units, done once).
- **Playlist** (`/playlist?list=...`): new videos in that playlist only. The whole
  playlist is read each run, since playlists aren't always newest first.
- **Single video** (`watch?v=`, `youtu.be/`, `/shorts/`, `/live/`): that one video is
  sent once, however old it is.

A link that can't be found on YouTube is logged as "Could not resolve channel" and
tried again next run.

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

## Uploading new videos to the MissionEngine API (optional)

After each run, the videos found in that run can be sent to the API's `/events`
endpoint: one `POST` per video, after logging in with the OAuth **password grant**.
It is off unless `upload.enabled` is `true` in `config.json`. Each video becomes a
body like this:

```json
{"url": "https://www.youtube.com/watch?v=...",
 "type": "video",
 "source": "Channel name",
 "title": "Video title",
 "description": "The video's YouTube description (first 500 characters)",
 "image_url": "https://i.ytimg.com/vi/<video id>/hqdefault.jpg",
 "country": "PH",
 "check_duplicate": true}
```

`country` is the country YouTube lists for the channel; channels that haven't set
one send no `country` at all, so the API works the country out itself. Any other text value
that comes out empty is left out too.

1. Fill in the `upload` block of `config.json` (see `config.example.json`):
   - `api_url` / `token_url` - the `/events` endpoint and the token endpoint
   - `include_grant_type` - `false` sends only `username` and `password` to the
     token endpoint (what the sample script does); `true` also sends
     `grant_type=password`, as the OAuth standard says
   - `scope`, `client_auth` - only needed if the API asks for them
   - `payload` - the JSON sent for each video. **Text** values are templates;
     these placeholders are filled in per video: `{video_url}`, `{video_title}`,
     `{video_description}`, `{video_tags}`, `{video_id}`, `{channel_name}`, `{channel_url}`,
     `{channel_country}`, `{published_at}`, `{fetched_at}`. Everything else in the
     text is sent as written (use `{{` and `}}` for literal braces). **true/false,
     numbers and objects** are sent exactly as you write them, e.g.
     `"publish": true`.
   - `description_max_chars` - the `description` value is cut to this length
   - `timeout_seconds` - per-request timeout
   - `resources_url` - where `--check-resources` reads from. If left out, it is `api_url` with `/events` replaced by `/resources`

   `tags` is not sent by default, so the API applies its own default behaviour. To send the
   tags the uploader gave the video on YouTube (a comma-separated list; costs 1 API unit
   per 50 videos), add `"tags": "{video_tags}"` to `payload`.

   Fields of `/events` that are **not** sent unless you add them to `payload`:
   `source_id`, `role`, `file_url`, `event_type`, `event_start`, `event_end`,
   `city`, `user`, `publish`, `process`. `publish` and `process` are left out on
   purpose, so the API's own defaults apply.
2. Set the secrets as environment variables (they never go in a file):
   ```
   setx UPLOAD_API_USERNAME "your_username"
   setx UPLOAD_API_PASSWORD "your_password"
   ```
   `UPLOAD_API_CLIENT_ID` and `UPLOAD_API_CLIENT_SECRET` are optional; set them
   only if the API requires a client login. Open a new terminal afterwards.
3. `pip install -r requirements.txt` (this adds `requests`).

URLs must be `https://` (plain `http://` is only allowed for `localhost`).

**Checking that it works**

- Every run ends with a summary line that says what the upload did, for example
  `uploaded 8 video(s) (2 from earlier runs)`, `nothing to upload`, or
  `upload FAILED (queued for retry)`. A successful login is logged as
  `Logged in to <host>`. The summary says nothing about uploads when they are off.
- Each video's request is logged: `POST <url> payload: {...}` with the exact JSON sent
  to `/events`, then `-> HTTP <status>`. The access token and password are never logged.
- To test the connection without scraping or sending anything, run
  `python yt_tracker.py --check-upload`. It logs in and reports whether that worked.
  It checks the login only; it can't check that the `/events` address exists
  without creating a record.

- To confirm the videos arrived, run `python yt_tracker.py --check-resources`. It
  reads resources back from the API (`GET /resources`, using the same login) and
  looks for the 10 most recent videos in `new_videos.csv`. It lists the first few
  resources it got, and says which of your videos were found and which were not.
  Options: `--resource-type TYPE` (default: the `type` your uploader sends, i.e.
  `video`; use `all` for every type), `--limit N` (per page, default 50) and
  `--pages N` (how many pages to scan, default 3). A video that isn't found may
  still be processing, may be further down than the pages scanned, or may have
  been refused (see `rejected_uploads.json`).

**Uploading the CSV on demand**

`upload_csv.py` (next to `yt_tracker.py`) sends every video in `new_videos.csv`
without scraping anything:

```
python upload_csv.py             # send them all, plus anything in pending_upload.json
python upload_csv.py --dry-run   # only count what would be sent
```

It uses the same `upload` settings and `UPLOAD_API_*` environment variables, and
stops with an error if `upload.enabled` is not `true`. Failures stay queued in
`pending_upload.json`, refused videos go to `rejected_uploads.json`, and the exit
code is 1 if the upload fails. Videos already waiting in `pending_upload.json` are
not sent twice. Sending a video that already arrived is harmless: the API refuses
the duplicate (`check_duplicate`) and it is skipped. The CSV has no description or
channel country, so those two fields are left out.

**When something goes wrong**
- **The API can't take it right now** (server error, no network, 403, 404, 429,
  or a login that fails): the videos that did not go through are kept in
  `pending_upload.json` and sent first on the next run.
- **The API refuses one video** (400, 409, 413 or 422 - bad data, or it already
  has that URL): it is logged, saved in `rejected_uploads.json` with the reason,
  and skipped, so it can't block the others. If 5 in a row are refused, the
  script stops and keeps them queued, because that points to wrong `payload`
  settings rather than bad data.
- The CSV and `state.json` are always updated before the upload. A failed upload
  makes the script exit with code 1 so Task Scheduler shows it.
- Sending is one request per video, so a big backfill takes a while.
- Channels saved in `channel_cache.json` by an older version are looked up once
  more so their country is known (1 API unit each, 100 for `/c/` URLs).
- A request can succeed even when its response is lost, so a video may rarely be
  sent twice. `check_duplicate` asks the API to catch that; the API should treat
  `url` as unique.

## Design

The script follows the object-oriented design principles from *Head First
Object-Oriented Analysis & Design*: encapsulate what varies, program to an
interface, favor composition, one responsibility per class.

| Class | Responsibility |
| --- | --- |
| `Tracker` | Runs one scrape. Knows nothing about Google, CSV or JSON. |
| `ChannelSource` (`GoogleSheetChannelSource`) | Where channel URLs come from |
| `AirtableLinkReader`, `GoogleSheetLinkCopier` | Adding new Airtable links to the Sheet before scraping |
| `VideoPlatform` (`YouTubePlatform`, `CachedPlatform`) | Find channels, list videos; the cache wraps the platform |
| `LookupStrategy` (`ById`, `ByHandle`, `ByUsername`, `ByCustomUrl`, `ByLegacyName`) | One class per YouTube channel URL style |
| `VideoStore` (`CsvVideoStore`) | Where results are saved |
| `VideoPublisher` (`ApiPublisher`, `RetryingPublisher`, `NullPublisher`) | Sending new videos to another API; the retrying wrapper queues failures |
| `AccessTokenProvider` (`PasswordGrantTokenProvider`) | Gets and caches OAuth tokens |
| `ResourceReader` (`ApiResourceReader`), `ResourceChecker` | `--check-resources`: reads resources back and looks for your videos |
| `CsvUploader` (in `upload_csv.py`) | Sends the saved videos through a `VideoPublisher`, on demand |
| `ScrapeHistory` | Which channels were scraped, and when |
| `Settings` | Contents of `config.json` |

To read channels from somewhere else, or save somewhere else, add one class that
implements the interface and swap it in `build_tracker()`. `Tracker` doesn't change.