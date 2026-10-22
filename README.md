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
4. Edit the CONFIG block at the top of `yt_tracker.py` (`SHEET_ID`, `SHEET_TAB`,
   `URL_COLUMN_HEADER`).
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
- `state.json` - last successful run time
- `channel_cache.json` - resolved channel IDs (saves API quota)
- `yt_tracker.log` - run log

All of the above, plus `service_account.json`, are git-ignored.
