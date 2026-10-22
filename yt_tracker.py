"""
YouTube new-video tracker: reads channel URLs from a PRIVATE Google Sheet,
finds videos published since the last successful run, and appends them to a
local CSV. Designed to run weekly from Windows Task Scheduler.

Requirements:
    pip install google-api-python-client google-auth

Setup (environment variable, set once in a normal terminal, then reopen it):
    setx YOUTUBE_API_KEY "your_youtube_api_key"

Edit the CONFIG section below with your sheet ID and service account key path.
"""

import csv
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

# ===================== CONFIG =====================
SHEET_ID = "PASTE_YOUR_SPREADSHEET_ID_HERE"      # the long ID in the sheet's URL
SHEET_TAB = "Sheet1"                              # tab name that holds the channel list
URL_COLUMN_HEADER = "channel_url"                 # header text of the column with channel URLs
SERVICE_ACCOUNT_FILE = r"service_account.json"    # path to key file (relative = next to this script)
FIRST_RUN_LOOKBACK_DAYS = 7                       # lookback on the very first run
# ==================================================

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_CSV = BASE_DIR / "new_videos.csv"
STATE_FILE = BASE_DIR / "state.json"
CACHE_FILE = BASE_DIR / "channel_cache.json"
LOG_FILE = BASE_DIR / "yt_tracker.log"

FIELDNAMES = ["fetched_at", "channel_url", "channel_name", "video_id",
              "video_title", "video_url", "published_at"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("yt_tracker")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # avoid Windows cp1252 crashes

API_KEY = os.environ.get("YOUTUBE_API_KEY")
if not API_KEY:
    log.error("YOUTUBE_API_KEY environment variable is not set.")
    sys.exit(1)

youtube = build("youtube", "v3", developerKey=API_KEY, cache_discovery=False)


# ---------- helpers ----------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------- Google Sheet input ----------

def read_channel_urls():
    key_path = Path(SERVICE_ACCOUNT_FILE)
    if not key_path.is_absolute():
        key_path = BASE_DIR / key_path

    creds = service_account.Credentials.from_service_account_file(
        str(key_path),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    resp = sheets.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range=f"'{SHEET_TAB}'"
    ).execute()

    rows = resp.get("values", [])
    if not rows:
        raise RuntimeError(f"Tab '{SHEET_TAB}' is empty.")

    header = [h.strip().lower() for h in rows[0]]
    if URL_COLUMN_HEADER.lower() not in header:
        raise RuntimeError(
            f"Column '{URL_COLUMN_HEADER}' not found. Headers seen: {rows[0]}"
        )
    col = header.index(URL_COLUMN_HEADER.lower())

    urls, seen = [], set()
    for r in rows[1:]:
        if len(r) > col and r[col].strip() and r[col].strip() not in seen:
            seen.add(r[col].strip())
            urls.append(r[col].strip())
    return urls


# ---------- channel resolution ----------

def extract_identifier(url: str):
    patterns = [
        ("channel_id", r"youtube\.com/channel/([\w-]+)"),
        ("handle",     r"youtube\.com/@([\w.-]+)"),
        ("username",   r"youtube\.com/user/([\w-]+)"),
        ("custom",     r"youtube\.com/c/([\w-]+)"),
    ]
    for id_type, pat in patterns:
        m = re.search(pat, url)
        if m:
            return id_type, m.group(1)
    return None, None


def resolve_channel(url: str):
    id_type, value = extract_identifier(url)
    if not id_type:
        return None

    params = {"part": "snippet,contentDetails"}
    if id_type == "channel_id":
        params["id"] = value
    elif id_type == "handle":
        params["forHandle"] = value
    elif id_type == "username":
        params["forUsername"] = value
    else:  # /c/ custom URL: search costs 100 units, but the result is cached
        s = youtube.search().list(part="snippet", q=value, type="channel", maxResults=1).execute()
        if not s.get("items"):
            return None
        params["id"] = s["items"][0]["snippet"]["channelId"]

    items = youtube.channels().list(**params).execute().get("items", [])
    if not items:
        return None
    item = items[0]
    return {
        "channel_id": item["id"],
        "name": item["snippet"]["title"],
        "uploads_playlist": item["contentDetails"]["relatedPlaylists"]["uploads"],
    }


# ---------- fetching new videos ----------

def get_videos_since(uploads_playlist: str, since: datetime):
    videos, page_token = [], None
    while True:
        resp = youtube.playlistItems().list(
            part="snippet,contentDetails",
            playlistId=uploads_playlist,
            maxResults=50,
            pageToken=page_token,
        ).execute()

        for it in resp.get("items", []):
            published = it["contentDetails"].get("videoPublishedAt")
            if not published:
                continue  # private/deleted video
            if parse_ts(published) <= since:
                return videos  # playlist is newest-first, so the rest are older
            videos.append({
                "video_id": it["contentDetails"]["videoId"],
                "video_title": it["snippet"]["title"],
                "published_at": published,
            })

        page_token = resp.get("nextPageToken")
        if not page_token:
            return videos


def existing_video_ids():
    if not OUTPUT_CSV.exists():
        return set()
    with open(OUTPUT_CSV, newline="", encoding="utf-8-sig") as f:
        return {row["video_id"] for row in csv.DictReader(f)}


# ---------- main ----------

def main():
    run_started = datetime.now(timezone.utc)

    state = load_json(STATE_FILE, {})
    since = parse_ts(state["last_run"]) if "last_run" in state \
        else run_started - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
    log.info("Run started. Fetching videos published after %s", since.isoformat())

    try:
        urls = read_channel_urls()
    except Exception as e:
        log.error("Could not read Google Sheet: %s", e)
        sys.exit(1)  # state untouched, so the next run still covers this gap
    log.info("Read %d channel URL(s) from the sheet", len(urls))

    cache = load_json(CACHE_FILE, {})
    seen_ids = existing_video_ids()
    new_rows, all_ok = [], True

    for url in urls:
        try:
            if url not in cache:
                info = resolve_channel(url)
                if not info:
                    log.warning("Could not resolve channel: %s", url)
                    all_ok = False
                    continue
                cache[url] = info
            info = cache[url]

            for v in get_videos_since(info["uploads_playlist"], since):
                if v["video_id"] in seen_ids:
                    continue
                seen_ids.add(v["video_id"])
                new_rows.append({
                    "fetched_at": run_started.isoformat(),
                    "channel_url": url,
                    "channel_name": info["name"],
                    "video_id": v["video_id"],
                    "video_title": v["video_title"],
                    "video_url": f"https://www.youtube.com/watch?v={v['video_id']}",
                    "published_at": v["published_at"],
                })
        except Exception as e:
            log.error("Error on %s: %s", url, e)
            all_ok = False

    save_json(CACHE_FILE, cache)

    if new_rows:
        is_new_file = not OUTPUT_CSV.exists() or OUTPUT_CSV.stat().st_size == 0
        # BOM on creation so Excel on Windows displays non-English titles correctly
        with open(OUTPUT_CSV, "a", newline="", encoding="utf-8-sig" if is_new_file else "utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if is_new_file:
                writer.writeheader()
            writer.writerows(new_rows)
    log.info("Appended %d new video(s) to %s", len(new_rows), OUTPUT_CSV.name)

    # Advance the checkpoint only if every channel succeeded; video_id dedup makes retries safe.
    if all_ok:
        save_json(STATE_FILE, {"last_run": run_started.isoformat()})
    else:
        log.warning("Some channels failed; keeping previous last_run so they are retried next run.")


if __name__ == "__main__":
    main()
