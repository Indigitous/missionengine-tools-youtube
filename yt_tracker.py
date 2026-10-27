"""
YouTube new-video tracker.

Reads channel URLs from a PRIVATE Google Sheet, finds videos published since each
channel was last scraped, and appends them to a local CSV. Designed to run weekly
from Windows Task Scheduler.

Requirements:
    pip install -r requirements.txt

Setup:
    setx YOUTUBE_API_KEY "your_youtube_api_key"      (then open a new terminal)
    copy config.example.json to config.json and fill in your sheet ID

Design (in the spirit of Head First OOA&D):
    Tracker           orchestrates one run; knows NOTHING about Google, CSV or JSON.
    ChannelSource     interface: where channel URLs come from   -> GoogleSheetChannelSource
    VideoPlatform     interface: find channels / list videos    -> YouTubePlatform
                                                                  CachedPlatform (decorator)
    VideoStore        interface: where results are saved        -> CsvVideoStore
    VideoPublisher    interface: where new videos are sent on   -> ApiPublisher
                                                                  RetryingPublisher (decorator)
                                                                  NullPublisher (uploads off)
    AccessTokenProvider  interface: OAuth tokens                -> PasswordGrantTokenProvider
    ScrapeHistory     remembers which channels were scraped, and when
    LookupStrategy    encapsulates what varies between channel URL styles
    Settings          the contents of config.json

    Swap Sheets for a CSV, or CSV for a database, by writing one new class that
    implements the interface. Tracker never changes.
"""

import argparse
import csv
import json
import logging
import os
import re
import string
import sys
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set
from urllib.parse import urlparse

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
OUTPUT_CSV = BASE_DIR / "new_videos.csv"
STATE_FILE = BASE_DIR / "state.json"
CACHE_FILE = BASE_DIR / "channel_cache.json"
LOG_FILE = BASE_DIR / "yt_tracker.log"
PENDING_FILE = BASE_DIR / "pending_upload.json"
REJECTED_FILE = BASE_DIR / "rejected_uploads.json"

log = logging.getLogger("yt_tracker")

# Older than any YouTube video; used as the cutoff when lookback is "all".
BEGINNING_OF_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)


# =====================================================================
# Errors: one per kind of failure that should stop the whole run
# =====================================================================

class ConfigError(Exception):
    """config.json (or the API key) is missing or invalid."""


class ChannelSourceError(Exception):
    """The list of channels could not be read."""


class PublishError(Exception):
    """An upload failed. `delivered` is how many records went through before it did."""

    def __init__(self, message: str, delivered: int = 0):
        super().__init__(message)
        self.delivered = delivered


# =====================================================================
# Domain objects: the "nouns" of the problem
# =====================================================================

@dataclass(frozen=True)
class Channel:
    channel_id: str
    name: str
    uploads_playlist: str
    country: str = ""          # ISO country code YouTube lists for the channel, if it set one


@dataclass(frozen=True)
class Video:
    video_id: str
    title: str
    published_at: str          # ISO 8601, as returned by YouTube
    description: str = ""      # not written to the CSV; available to the upload

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"


@dataclass(frozen=True)
class ScrapeRecord:
    """One row of output: a video, plus how we found it."""
    fetched_at: str
    channel_url: str
    channel: Channel
    video: Video

    def to_row(self) -> Dict[str, str]:
        return {
            "fetched_at": self.fetched_at,
            "channel_url": self.channel_url,
            "channel_name": self.channel.name,
            "video_id": self.video.video_id,
            "video_title": self.video.title,
            "video_url": self.video.url,
            "published_at": self.video.published_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScrapeRecord":
        """Inverse of dataclasses.asdict(); used to reload records queued for upload."""
        return cls(data["fetched_at"], data["channel_url"],
                   Channel(**data["channel"]), Video(**data["video"]))


# Column names of one output row, in order. Shared by the CSV and the upload.
ROW_FIELDS = ["fetched_at", "channel_url", "channel_name", "video_id",
              "video_title", "video_url", "published_at"]


@dataclass
class PublishReport:
    """What one upload did. Returned by VideoPublisher.publish()."""
    attempted: bool = True      # False when uploads are switched off
    sent: int = 0               # videos the API accepted
    rejected: int = 0           # videos the API refused (saved in rejected_uploads.json)
    carried_over: int = 0       # of `sent`, how many were left over from earlier runs


@dataclass
class RunSummary:
    new: int = 0
    known: int = 0
    duplicate: int = 0
    failed: int = 0
    videos_added: int = 0
    upload_failed: bool = False
    upload: Optional[PublishReport] = None

    def describe(self) -> str:
        text = (f"Summary: {self.new} new channel(s), {self.known} known channel(s), "
                f"{self.duplicate} duplicate URL(s), {self.failed} failed; "
                f"{self.videos_added} video(s) added")
        if self.upload_failed:
            text += "; upload FAILED (queued for retry)"
        elif self.upload is not None and self.upload.attempted:
            up = self.upload
            if up.sent == 0 and up.rejected == 0:
                text += "; nothing to upload"
            else:
                text += f"; uploaded {up.sent} video(s)"
                if up.carried_over:
                    text += f" ({up.carried_over} from earlier runs)"
                if up.rejected:
                    text += f", {up.rejected} rejected by the API"
        return text


def parse_ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# =====================================================================
# Settings: everything that comes from config.json
# =====================================================================

def _require_secure_url(name: str, url: str) -> None:
    """The password grant sends real credentials, so plain http is only allowed for local testing."""
    if url.startswith("https://") or re.match(r"http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", url):
        return
    raise ConfigError(f"upload.{name} must start with https:// (got {url!r}).")


def _positive_int(name: str, value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"upload.{name} must be a whole number (got {value!r}).") from e
    if number < 1:
        raise ConfigError(f"upload.{name} must be at least 1.")
    return number


# Placeholders usable in the 'payload' templates of config.json.
TEMPLATE_FIELDS = ROW_FIELDS + ["video_description", "channel_country"]

# The body posted to the API for each video. Text values are templates: {video_title} and
# friends are filled in per video, and the rest of the text is sent as written. True/False,
# numbers and objects are sent exactly as given. A text value that comes out empty (say, a
# channel with no country) is left out of the request.
DEFAULT_PAYLOAD = {
    "url": "{video_url}",
    "type": "resource",
    "source": "{channel_name}",
    "title": "{video_title}",
    "description": "{video_description}",
    "tags": "youtube",
    "image_url": "https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    "country": "{channel_country}",
    "check_duplicate": True,
}


def _check_payload_value(key: str, value) -> None:
    if isinstance(value, (bool, int, float, dict, list)):
        return   # sent exactly as written
    if not isinstance(value, str):
        raise ConfigError(
            f"upload.payload.{key} must be text, true/false, a number or an object (got {value!r}).")
    try:
        names = [name for _, name, _, _ in string.Formatter().parse(value) if name is not None]
    except ValueError as e:
        raise ConfigError(f"upload.payload.{key} is not a valid template: {e}") from e
    unknown = sorted(set(names) - set(TEMPLATE_FIELDS))
    if unknown:
        raise ConfigError(
            f"upload.payload.{key} uses unknown placeholder(s) {unknown}; valid ones: {TEMPLATE_FIELDS}")


@dataclass(frozen=True)
class UploadSettings:
    """The optional 'upload' block of config.json. Disabled unless enabled is true."""
    enabled: bool = False
    api_url: str = ""
    token_url: str = ""
    scope: str = ""
    client_auth: str = "basic"          # "basic" = HTTP Basic header, "body" = client_id/secret in the form
    include_grant_type: bool = True     # false = send only username + password to the token endpoint
    payload: Dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_PAYLOAD))
    description_max_chars: int = 500    # the "description" value is cut to this length
    timeout_seconds: int = 30

    @classmethod
    def from_dict(cls, raw) -> "UploadSettings":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ConfigError("'upload' in config.json must be an object.")
        if not raw.get("enabled", False):
            return cls()

        if "event" in raw:
            raise ConfigError(
                "upload.event was renamed to upload.payload. Replace it with the 'payload' block "
                "from config.example.json.")

        api_url = str(raw.get("api_url", "")).strip()
        token_url = str(raw.get("token_url", "")).strip()
        if not api_url or not token_url:
            raise ConfigError("upload.api_url and upload.token_url must both be set when upload.enabled is true.")
        _require_secure_url("api_url", api_url)
        _require_secure_url("token_url", token_url)

        client_auth = str(raw.get("client_auth", "basic")).strip().lower()
        if client_auth not in ("basic", "body"):
            raise ConfigError('upload.client_auth must be "basic" or "body".')

        include_grant_type = raw.get("include_grant_type", True)
        if not isinstance(include_grant_type, bool):
            raise ConfigError("upload.include_grant_type must be true or false.")

        payload = raw.get("payload")
        if payload is None:
            payload = dict(DEFAULT_PAYLOAD)
        elif not isinstance(payload, dict) or not payload:
            raise ConfigError("upload.payload must be an object of API field names and values.")
        for key, value in payload.items():
            _check_payload_value(key, value)

        return cls(
            enabled=True,
            api_url=api_url,
            token_url=token_url,
            scope=str(raw.get("scope", "")).strip(),
            client_auth=client_auth,
            include_grant_type=include_grant_type,
            payload=dict(payload),
            description_max_chars=_positive_int("description_max_chars", raw.get("description_max_chars", 500)),
            timeout_seconds=_positive_int("timeout_seconds", raw.get("timeout_seconds", 30)),
        )


@dataclass(frozen=True)
class Settings:
    sheet_id: str
    sheet_tab: str = "Sheet1"
    url_column_header: str = "channel_url"
    service_account_file: str = "service_account.json"
    new_channel_lookback_days: Optional[int] = 7   # None means "all": every video, from the beginning
    upload: UploadSettings = field(default_factory=UploadSettings)

    @property
    def new_channel_lookback(self) -> Optional[timedelta]:
        days = self.new_channel_lookback_days
        return None if days is None else timedelta(days=days)

    @staticmethod
    def _parse_lookback(value) -> Optional[int]:
        if isinstance(value, str) and value.strip().lower() == "all":
            return None
        try:
            days = int(value)
        except (TypeError, ValueError) as e:
            raise ConfigError(
                f"'new_channel_lookback_days' must be a whole number of days or \"all\" (got {value!r}).") from e
        if days < 1:
            raise ConfigError("'new_channel_lookback_days' must be at least 1 (or \"all\").")
        return days

    @classmethod
    def from_file(cls, path: Path) -> "Settings":
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as e:
            raise ConfigError(
                f"Config file not found: {path} (copy config.example.json to config.json)") from e
        except json.JSONDecodeError as e:
            raise ConfigError(f"config.json is not valid JSON: {e}") from e

        sheet_id = str(cfg.get("sheet_id", "")).strip()
        if not sheet_id or sheet_id.startswith("PASTE_"):
            raise ConfigError("Set 'sheet_id' in config.json to your Google Sheet's ID.")

        # "first_run_lookback_days" is the older name for the same setting.
        lookback = cfg.get("new_channel_lookback_days", cfg.get("first_run_lookback_days", 7))
        return cls(
            sheet_id=sheet_id,
            sheet_tab=cfg.get("sheet_tab", cls.sheet_tab),
            url_column_header=cfg.get("url_column_header", cls.url_column_header),
            service_account_file=cfg.get("service_account_file", cls.service_account_file),
            new_channel_lookback_days=cls._parse_lookback(lookback),
            upload=UploadSettings.from_dict(cfg.get("upload")),
        )


def load_api_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        raise ConfigError("YOUTUBE_API_KEY environment variable is not set.")
    return key


@dataclass(frozen=True)
class UploadCredentials:
    """Secrets for the upload API. They come from environment variables, never from a file."""
    username: str
    password: str
    client_id: Optional[str] = None
    client_secret: Optional[str] = None

    def __repr__(self) -> str:   # keep secrets out of logs and tracebacks
        return f"UploadCredentials(username={self.username!r}, password=***)"


def load_upload_credentials() -> UploadCredentials:
    username = os.environ.get("UPLOAD_API_USERNAME")
    password = os.environ.get("UPLOAD_API_PASSWORD")
    if not username or not password:
        raise ConfigError("Uploads are enabled, so UPLOAD_API_USERNAME and UPLOAD_API_PASSWORD "
                          "environment variables must be set.")
    return UploadCredentials(
        username=username,
        password=password,
        client_id=os.environ.get("UPLOAD_API_CLIENT_ID") or None,
        client_secret=os.environ.get("UPLOAD_API_CLIENT_SECRET") or None,
    )


# =====================================================================
# Persistence helper: the one place that knows how JSON files are read/written
# =====================================================================

class JsonFile:
    def __init__(self, path: Path):
        self._path = path

    def load(self, default):
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default

    def save(self, data) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)   # atomic: a crash never leaves a half-written file


# =====================================================================
# Interface 1: where do channel URLs come from?
# =====================================================================

class ChannelSource(ABC):
    @abstractmethod
    def channel_urls(self) -> List[str]:
        """Return de-duplicated channel URLs. Raise ChannelSourceError on failure."""


class GoogleSheetChannelSource(ChannelSource):
    def __init__(self, sheet_id: str, tab: str, url_column_header: str, credentials_file: Path):
        self._sheet_id = sheet_id
        self._tab = tab
        self._header = url_column_header
        self._credentials_file = credentials_file

    def channel_urls(self) -> List[str]:
        try:
            rows = self._read_rows()
        except ChannelSourceError:
            raise
        except Exception as e:
            raise ChannelSourceError(f"Could not read Google Sheet: {e}") from e
        return self._extract_urls(rows)

    def _read_rows(self) -> List[List[str]]:
        creds = service_account.Credentials.from_service_account_file(
            str(self._credentials_file),
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
        response = sheets.spreadsheets().values().get(
            spreadsheetId=self._sheet_id, range=f"'{self._tab}'"
        ).execute()
        return response.get("values", [])

    def _extract_urls(self, rows: List[List[str]]) -> List[str]:
        if not rows:
            raise ChannelSourceError(f"Tab '{self._tab}' is empty.")

        header = [h.strip().lower() for h in rows[0]]
        if self._header.lower() not in header:
            raise ChannelSourceError(
                f"Column '{self._header}' not found. Headers seen: {rows[0]}")
        col = header.index(self._header.lower())

        urls, seen = [], set()
        for row in rows[1:]:
            if len(row) > col:
                url = row[col].strip()
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)
        return urls


# =====================================================================
# Interface 2: how do we find channels and their videos?
# =====================================================================

class VideoPlatform(ABC):
    @abstractmethod
    def find_channel(self, url: str) -> Optional[Channel]:
        """Resolve a channel URL, or return None if it can't be resolved."""

    @abstractmethod
    def videos_since(self, channel: Channel, since: datetime) -> List[Video]:
        """Videos published strictly after `since`, newest first."""


class LookupStrategy(ABC):
    """
    Encapsulates what varies: YouTube has four URL styles, and each is looked
    up differently. Supporting a fifth style means adding one class here.
    """
    pattern: str = ""

    def extract(self, url: str) -> Optional[str]:
        match = re.search(self.pattern, url)
        return match.group(1) if match else None

    @abstractmethod
    def params(self, youtube, value: str) -> Optional[dict]:
        """Arguments for channels().list(), or None if the channel doesn't exist."""


class ById(LookupStrategy):
    pattern = r"youtube\.com/channel/([\w-]+)"

    def params(self, youtube, value):
        return {"id": value}


class ByHandle(LookupStrategy):
    pattern = r"youtube\.com/@([\w.-]+)"

    def params(self, youtube, value):
        return {"forHandle": value}


class ByUsername(LookupStrategy):
    pattern = r"youtube\.com/user/([\w-]+)"

    def params(self, youtube, value):
        return {"forUsername": value}


class ByCustomUrl(LookupStrategy):
    pattern = r"youtube\.com/c/([\w-]+)"

    def params(self, youtube, value):
        # Only search can resolve /c/ URLs. It costs 100 quota units, which is
        # why CachedPlatform exists.
        found = youtube.search().list(
            part="snippet", q=value, type="channel", maxResults=1).execute()
        items = found.get("items", [])
        return {"id": items[0]["snippet"]["channelId"]} if items else None


class YouTubePlatform(VideoPlatform):
    """The only class that knows what the YouTube Data API's responses look like."""

    def __init__(self, api_key: str, strategies: Optional[List[LookupStrategy]] = None):
        self._youtube = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
        self._strategies = strategies or [ById(), ByHandle(), ByUsername(), ByCustomUrl()]

    def find_channel(self, url: str) -> Optional[Channel]:
        for strategy in self._strategies:
            value = strategy.extract(url)
            if value is None:
                continue
            params = strategy.params(self._youtube, value)
            if params is None:
                return None
            items = self._youtube.channels().list(
                part="snippet,contentDetails", **params).execute().get("items", [])
            if not items:
                return None
            item = items[0]
            return Channel(
                channel_id=item["id"],
                name=item["snippet"]["title"],
                uploads_playlist=item["contentDetails"]["relatedPlaylists"]["uploads"],
                country=item["snippet"].get("country", ""),
            )
        return None

    def videos_since(self, channel: Channel, since: datetime) -> List[Video]:
        videos: List[Video] = []
        page_token = None
        while True:
            response = self._youtube.playlistItems().list(
                part="snippet,contentDetails",
                playlistId=channel.uploads_playlist,
                maxResults=50,
                pageToken=page_token,
            ).execute()

            for item in response.get("items", []):
                published = item["contentDetails"].get("videoPublishedAt")
                if not published:
                    continue   # private or deleted video
                if parse_ts(published) <= since:
                    return videos   # newest-first, so everything after this is older
                videos.append(Video(
                    video_id=item["contentDetails"]["videoId"],
                    title=item["snippet"]["title"],
                    published_at=published,
                    description=item["snippet"].get("description", ""),
                ))

            page_token = response.get("nextPageToken")
            if not page_token:
                return videos


class CachedPlatform(VideoPlatform):
    """
    Decorator: wraps any VideoPlatform and remembers resolved channels on disk,
    so each URL is only looked up once. Tracker can't tell the difference.
    """

    def __init__(self, inner: VideoPlatform, cache_file: JsonFile):
        self._inner = inner
        self._file = cache_file
        self._channels: Dict[str, Channel] = {}
        self._stale: Set[str] = set()   # cached before channels had a country: look up once more
        for url, fields in cache_file.load({}).items():
            try:
                self._channels[url] = Channel(**fields)
            except TypeError:
                continue   # malformed entry: just look it up again
            if "country" not in fields:
                self._stale.add(url)

    def find_channel(self, url: str) -> Optional[Channel]:
        cached = self._channels.get(url)
        if cached is not None and url not in self._stale:
            return cached
        try:
            channel = self._inner.find_channel(url)
        except Exception:
            if cached is None:
                raise
            log.warning("Could not refresh cached channel %s; using the saved copy", url)
            return cached
        if channel is None:
            return cached   # nothing came back: keep what we had (still None if never cached)
        self._channels[url] = channel
        self._stale.discard(url)
        self._file.save({u: asdict(c) for u, c in self._channels.items()})
        return channel

    def videos_since(self, channel: Channel, since: datetime) -> List[Video]:
        return self._inner.videos_since(channel, since)

    def cached_channel_ids(self) -> Set[str]:
        return {c.channel_id for c in self._channels.values()}


# =====================================================================
# Interface 3: where do results go?
# =====================================================================

class VideoStore(ABC):
    @abstractmethod
    def known_video_ids(self) -> Set[str]:
        """IDs of videos already saved, so they are never saved twice."""

    @abstractmethod
    def append(self, records: List[ScrapeRecord]) -> None:
        """Add records without touching the ones already there."""


class CsvVideoStore(VideoStore):
    FIELDNAMES = ROW_FIELDS

    def __init__(self, path: Path):
        self._path = path

    def known_video_ids(self) -> Set[str]:
        if not self._path.exists():
            return set()
        with open(self._path, newline="", encoding="utf-8-sig") as f:
            return {row["video_id"] for row in csv.DictReader(f)}

    def append(self, records: List[ScrapeRecord]) -> None:
        if not records:
            return
        is_new_file = not self._path.exists() or self._path.stat().st_size == 0
        # BOM only when creating the file, so Excel on Windows shows non-English titles.
        encoding = "utf-8-sig" if is_new_file else "utf-8"
        with open(self._path, "a", newline="", encoding=encoding) as f:
            writer = csv.DictWriter(f, fieldnames=self.FIELDNAMES)
            if is_new_file:
                writer.writeheader()
            writer.writerows(r.to_row() for r in records)


# =====================================================================
# Interface 4: where else do new videos go? (an upload to another API)
# =====================================================================

class VideoPublisher(ABC):
    @abstractmethod
    def publish(self, records: List[ScrapeRecord]) -> PublishReport:
        """Send records onward. On failure raise PublishError(delivered=<how many got through>)."""

    def check_connection(self) -> str:
        """Test the connection without sending anything. Raise PublishError if it fails."""
        return "No connection check is available for this publisher."


class NullPublisher(VideoPublisher):
    """Null Object: used when uploads are off, so Tracker never has to check."""

    def publish(self, records: List[ScrapeRecord]) -> PublishReport:
        return PublishReport(attempted=False)

    def check_connection(self) -> str:
        return "Uploads are switched off (upload.enabled is not true in config.json)."


def _describe_error(response) -> str:
    """A short, safe description of a failed HTTP response (OAuth error fields if present)."""
    try:
        body = response.json()
        if isinstance(body, dict):
            parts = [str(body[k]) for k in ("error", "error_description", "message", "detail") if body.get(k)]
            if parts:
                return " - ".join(parts)[:200]
    except ValueError:
        pass
    return (response.text or "").strip()[:200]


class AccessTokenProvider(ABC):
    @abstractmethod
    def get_token(self) -> str:
        """A currently valid access token. Raise PublishError if one can't be obtained."""

    @abstractmethod
    def invalidate(self) -> None:
        """Forget the cached token (the API said it is no longer good)."""


class PasswordGrantTokenProvider(AccessTokenProvider):
    """OAuth 2.0 resource owner password grant (RFC 6749, section 4.3), with token caching."""

    EXPIRY_MARGIN = timedelta(seconds=60)

    def __init__(
        self,
        token_url: str,
        credentials: UploadCredentials,
        scope: str = "",
        client_auth: str = "basic",
        send_grant_type: bool = True,   # some servers want only username + password
        timeout: int = 30,
        session=None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._token_url = token_url
        self._credentials = credentials
        self._scope = scope
        self._client_auth = client_auth
        self._send_grant_type = send_grant_type
        self._timeout = timeout
        self._session = session or requests.Session()
        self._clock = clock
        self._token: Optional[str] = None
        self._expires_at: Optional[datetime] = None

    def get_token(self) -> str:
        now = self._clock()
        if self._token and (self._expires_at is None or now < self._expires_at):
            return self._token
        self._fetch(now)
        return self._token

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = None

    def _fetch(self, now: datetime) -> None:
        creds = self._credentials
        form = {"username": creds.username, "password": creds.password}
        if self._send_grant_type:
            form["grant_type"] = "password"
        if self._scope:
            form["scope"] = self._scope

        basic_auth = None
        if creds.client_id:
            if self._client_auth == "basic":
                basic_auth = (creds.client_id, creds.client_secret or "")
            else:
                form["client_id"] = creds.client_id
                if creds.client_secret:
                    form["client_secret"] = creds.client_secret

        try:
            response = self._session.post(
                self._token_url, data=form, auth=basic_auth, timeout=self._timeout)
        except requests.RequestException as e:
            raise PublishError(f"Could not reach the token endpoint: {e}") from e
        if response.status_code != 200:
            raise PublishError(
                f"Token request failed: HTTP {response.status_code} {_describe_error(response)}")

        try:
            body = response.json()
            token = body["access_token"]
        except (ValueError, KeyError, TypeError) as e:
            raise PublishError("Token response did not contain an access_token.") from e

        try:
            lifetime = timedelta(seconds=int(body.get("expires_in")))
            self._expires_at = now + lifetime - self.EXPIRY_MARGIN
        except (TypeError, ValueError):
            self._expires_at = None   # no expiry given: keep the token until the API says 401
        self._token = token
        log.info("Logged in to %s", urlparse(self._token_url).netloc)


class ApiPublisher(VideoPublisher):
    """
    POSTs one JSON body per video to upload.api_url, authenticating
    with a bearer token. What goes wrong is sorted into three kinds:

      accepted        any 2xx.
      rejected        400/409/413/422: this one video is the problem (bad data, or the
                      API already has it). It is logged, saved in rejected_uploads.json
                      and skipped, so it can't block the videos behind it.
      retry later     everything else (401 after a fresh token, 403, 404, 429, 5xx,
                      network errors): not the video's fault. Stop and raise
                      PublishError so the rest stays queued for the next run.
    """

    REJECTED_CODES = {400, 409, 413, 422}
    MAX_CONSECUTIVE_REJECTIONS = 5   # that many in a row means our settings are wrong, not the data

    def __init__(self, settings: UploadSettings, tokens: AccessTokenProvider,
                 session=None, rejected_file: Optional[JsonFile] = None):
        self._url = settings.api_url
        self._payload = settings.payload
        self._max_description = settings.description_max_chars
        self._timeout = settings.timeout_seconds
        self._tokens = tokens
        self._session = session or requests.Session()
        self._rejected_file = rejected_file

    def publish(self, records: List[ScrapeRecord]) -> PublishReport:
        rejected: List[tuple] = []   # (record, reason) for videos the API refused
        streak: List[tuple] = []     # (index, record, reason) refused back to back
        try:
            for index, record in enumerate(records):
                try:
                    reason = self._send(record)
                except PublishError as e:
                    e.delivered = index   # everything before this one was handled
                    raise
                if reason is None:
                    rejected.extend((r, why) for _, r, why in streak)
                    streak = []
                    continue
                streak.append((index, record, reason))
                if len(streak) >= self.MAX_CONSECUTIVE_REJECTIONS:
                    raise PublishError(
                        f"The API rejected {len(streak)} videos in a row ({reason}). Check the "
                        f"'payload' settings; stopping so nothing is discarded.",
                        delivered=streak[0][0])
            rejected.extend((r, why) for _, r, why in streak)
        finally:
            self._remember_rejections(rejected)
        return PublishReport(sent=len(records) - len(rejected), rejected=len(rejected))

    def check_connection(self) -> str:
        self._tokens.get_token()   # raises PublishError if the login fails
        return f"Login to the upload API works. Videos would be sent to {self._url}"

    def _body_for(self, record: ScrapeRecord) -> Dict[str, Any]:
        values = dict(record.to_row(), video_description=record.video.description,
                      channel_country=record.channel.country)
        body: Dict[str, Any] = {}
        for key, template in self._payload.items():
            if not isinstance(template, str):
                body[key] = template   # true/false, numbers and objects go out exactly as written
                continue
            filled_from = values
            if key == "tags":   # tags are comma separated, so a comma inside a value would split it
                filled_from = {k: v.replace(",", " ") for k, v in values.items()}
            text = template.format_map(filled_from)
            if key == "description":
                text = text[:self._max_description]
            if text.strip():   # an empty value (no description, no country) is simply not sent
                body[key] = text
        return body

    def _send(self, record: ScrapeRecord) -> Optional[str]:
        """None if the API accepted the video, or the reason if it refused this one."""
        body = self._body_for(record)
        for attempt in (1, 2):
            token = self._tokens.get_token()
            try:
                response = self._session.post(
                    self._url, json=body, timeout=self._timeout,
                    headers={"Authorization": f"Bearer {token}"})
            except requests.RequestException as e:
                raise PublishError(f"Could not reach the API: {e}") from e

            status = response.status_code
            if status == 401 and attempt == 1:
                self._tokens.invalidate()   # token expired early or was revoked: get a fresh one, once
                continue
            if 200 <= status < 300:
                return None
            reason = f"HTTP {status} {_describe_error(response)}".strip()
            if status in self.REJECTED_CODES:
                return reason
            raise PublishError(f"API could not take the upload: {reason}")

    def _remember_rejections(self, rejected: List[tuple]) -> None:
        for record, reason in rejected:
            log.warning("API rejected %s: %s", record.video.url, reason)
        if rejected and self._rejected_file is not None:
            kept = self._rejected_file.load([])
            when = datetime.now(timezone.utc).isoformat()
            kept.extend({"rejected_at": when, "reason": why, "record": asdict(r)} for r, why in rejected)
            self._rejected_file.save(kept)


class RetryingPublisher(VideoPublisher):
    """
    Decorator: keeps records that could not be delivered in a pending file and
    sends them first on the next run. Nothing is lost if the API is down.
    """

    def __init__(self, inner: VideoPublisher, pending_file: JsonFile):
        self._inner = inner
        self._file = pending_file

    def publish(self, records: List[ScrapeRecord]) -> PublishReport:
        pending = self._load_pending()
        to_send = pending + list(records)
        if not to_send:
            log.info("Nothing to upload.")
            return PublishReport()

        try:
            report = self._inner.publish(to_send)
        except PublishError as e:
            remaining = to_send[e.delivered:]
            self._save_pending(remaining)
            raise PublishError(
                f"{e} ({len(remaining)} record(s) queued for the next run)",
                delivered=e.delivered) from e

        self._save_pending([])
        report.carried_over = len(pending)
        log.info("Uploaded %d video(s) (%d left over from earlier runs, %d rejected by the API)",
                 report.sent, report.carried_over, report.rejected)
        return report

    def check_connection(self) -> str:
        return self._inner.check_connection()

    def _load_pending(self) -> List[ScrapeRecord]:
        loaded = []
        for item in self._file.load([]):
            try:
                loaded.append(ScrapeRecord.from_dict(item))
            except (KeyError, TypeError):
                log.warning("Ignoring an unreadable record in the pending upload file")
        return loaded

    def _save_pending(self, records: List[ScrapeRecord]) -> None:
        self._file.save([asdict(r) for r in records])


# =====================================================================
# ScrapeHistory: "has this channel been scraped before, and when?"
# =====================================================================

class ScrapeHistory:
    """Per-channel checkpoints, keyed by channel ID (so URL changes don't matter)."""

    def __init__(self, state_file: JsonFile):
        self._file = state_file
        data = state_file.load({})
        self._channels: Dict[str, str] = dict(data.get("channels", {}))
        # Older versions kept a single global "last_run" and no per-channel data.
        self._legacy_last_run: Optional[str] = (
            data.get("last_run") if "channels" not in data else None)

    def has_scraped(self, channel_id: str) -> bool:
        return channel_id in self._channels

    def last_scraped(self, channel_id: str) -> Optional[datetime]:
        stamp = self._channels.get(channel_id)
        return parse_ts(stamp) if stamp else None

    def mark_scraped(self, channel_id: str, when: datetime) -> None:
        self._channels[channel_id] = when.isoformat()

    def migrate_legacy(self, cached_channel_ids: Iterable[str]) -> None:
        """Channels we had already resolved under the old scheme count as scraped."""
        if self._legacy_last_run is None:
            return
        for channel_id in cached_channel_ids:
            self._channels.setdefault(channel_id, self._legacy_last_run)
        self._legacy_last_run = None

    def save(self, run_started: datetime) -> None:
        self._file.save({"last_run": run_started.isoformat(), "channels": self._channels})

    def __len__(self) -> int:
        return len(self._channels)


# =====================================================================
# Tracker: orchestrates a run. Depends only on the interfaces above.
# =====================================================================

class Tracker:
    def __init__(
        self,
        source: ChannelSource,
        platform: VideoPlatform,
        store: VideoStore,
        history: ScrapeHistory,
        new_channel_lookback: Optional[timedelta],   # None = fetch a new channel's entire history
        publisher: Optional[VideoPublisher] = None,   # None = no upload
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._source = source
        self._platform = platform
        self._store = store
        self._history = history
        self._new_channel_lookback = new_channel_lookback
        self._publisher = publisher or NullPublisher()
        self._clock = clock

    def run(self) -> RunSummary:
        run_started = self._clock()
        lookback_text = ("their entire history" if self._new_channel_lookback is None
                         else f"{self._new_channel_lookback.days} day(s)")
        log.info("Run started. %d channel(s) already tracked; new channels fetch %s",
                 len(self._history), lookback_text)

        urls = self._source.channel_urls()   # on failure nothing below runs, history untouched
        log.info("Read %d channel URL(s)", len(urls))

        seen_video_ids = self._store.known_video_ids()
        handled_channel_ids: Set[str] = set()
        records: List[ScrapeRecord] = []
        summary = RunSummary()

        for url in urls:
            try:
                channel = self._platform.find_channel(url)
                if channel is None:
                    log.warning("Could not resolve channel: %s", url)
                    summary.failed += 1
                    continue

                if channel.channel_id in handled_channel_ids:
                    log.info("Skipping %s: same channel already handled this run", url)
                    summary.duplicate += 1
                    continue

                since, is_new = self._starting_point(channel, run_started)
                log.info("[%s] %s - videos published after %s",
                         "NEW" if is_new else "KNOWN", channel.name, since.isoformat())

                for video in self._platform.videos_since(channel, since):
                    if video.video_id in seen_video_ids:
                        continue
                    seen_video_ids.add(video.video_id)
                    records.append(ScrapeRecord(run_started.isoformat(), url, channel, video))

                # Reached only if the whole channel succeeded. A channel that errors
                # keeps its old checkpoint (or stays "new") and is retried alone.
                self._history.mark_scraped(channel.channel_id, run_started)
                handled_channel_ids.add(channel.channel_id)
                if is_new:
                    summary.new += 1
                else:
                    summary.known += 1
            except Exception as e:
                log.error("Error on %s: %s", url, e)
                summary.failed += 1

        # Save results first, then checkpoints: a crash in between re-fetches
        # (deduplicated by video ID) instead of losing videos.
        self._store.append(records)
        summary.videos_added = len(records)
        self._history.save(run_started)

        # Upload last, after everything is safely on disk. A failed upload never undoes
        # the scrape: the publisher queues what didn't go through for the next run.
        try:
            summary.upload = self._publisher.publish(records)
        except PublishError as e:
            log.error("Upload failed: %s", e)
            summary.upload_failed = True
        return summary

    def _starting_point(self, channel: Channel, run_started: datetime):
        """Returns (fetch videos after this time, is this channel new?)."""
        last = self._history.last_scraped(channel.channel_id)
        if last is not None:
            return last, False
        if self._new_channel_lookback is None:
            return BEGINNING_OF_TIME, True
        return run_started - self._new_channel_lookback, True


# =====================================================================
# Composition root: the only place that knows which concrete classes are used
# =====================================================================

def build_publisher(upload: UploadSettings) -> VideoPublisher:
    if not upload.enabled:
        return NullPublisher()
    tokens = PasswordGrantTokenProvider(
        token_url=upload.token_url,
        credentials=load_upload_credentials(),
        scope=upload.scope,
        client_auth=upload.client_auth,
        send_grant_type=upload.include_grant_type,
        timeout=upload.timeout_seconds,
    )
    publisher = ApiPublisher(upload, tokens, rejected_file=JsonFile(REJECTED_FILE))
    return RetryingPublisher(publisher, JsonFile(PENDING_FILE))


def build_tracker(settings: Settings, api_key: str) -> Tracker:
    key_file = Path(settings.service_account_file)
    if not key_file.is_absolute():
        key_file = BASE_DIR / key_file

    platform = CachedPlatform(YouTubePlatform(api_key), JsonFile(CACHE_FILE))
    history = ScrapeHistory(JsonFile(STATE_FILE))
    history.migrate_legacy(platform.cached_channel_ids())

    return Tracker(
        source=GoogleSheetChannelSource(
            settings.sheet_id, settings.sheet_tab, settings.url_column_header, key_file),
        platform=platform,
        store=CsvVideoStore(OUTPUT_CSV),
        history=history,
        new_channel_lookback=settings.new_channel_lookback,
        publisher=build_publisher(settings.upload),
    )


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # avoid Windows cp1252 crashes


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Find new videos on the YouTube channels listed in a Google Sheet.")
    parser.add_argument("--check-upload", action="store_true",
                        help="only test the login to the upload API, then exit; nothing is scraped or sent")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    configure_logging()
    try:
        settings = Settings.from_file(CONFIG_FILE)
        if args.check_upload:
            log.info("%s", build_publisher(settings.upload).check_connection())
            return 0
        summary = build_tracker(settings, load_api_key()).run()
    except (ConfigError, ChannelSourceError) as e:
        log.error("%s", e)
        return 1
    except PublishError as e:
        log.error("Connection check failed: %s", e)
        return 1
    except Exception:
        log.exception("Unexpected failure")
        return 1

    log.info("%s", summary.describe())
    if summary.failed:
        log.warning("Failed channels keep their previous checkpoint and will be retried next run.")
    return 1 if summary.upload_failed else 0   # non-zero shows up in Task Scheduler's Last Run Result


if __name__ == "__main__":
    sys.exit(main())