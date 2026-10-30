"""
Upload every video in new_videos.csv to the API, on demand. No scraping.

    python upload_csv.py             send them all (plus anything waiting in pending_upload.json)
    python upload_csv.py --dry-run   only say how many would be sent

Uses the same config.json (upload block) and UPLOAD_API_* environment variables as
yt_tracker.py. The API refuses URLs it already has when check_duplicate is true, so
sending a video twice is harmless: it lands in rejected_uploads.json and is skipped.
The CSV has no description or channel country, so those fields are left out.
"""
import argparse
import sys
from typing import List, Optional

import yt_tracker as yt


class CsvUploader:
    """Sends the saved videos through a VideoPublisher; knows nothing about HTTP."""

    def __init__(self, store: yt.VideoStore, publisher: yt.VideoPublisher, pending: yt.JsonFile):
        self._store = store
        self._publisher = publisher
        self._pending = pending

    def records_to_send(self) -> List[yt.ScrapeRecord]:
        # Videos already waiting in pending_upload.json are sent by the publisher itself.
        queued = {item.get("video", {}).get("video_id") for item in self._pending.load([])
                  if isinstance(item, dict)}
        return [r for r in self._store.all_records() if r.video.video_id not in queued]

    def run(self) -> yt.PublishReport:
        return self._publisher.publish(self.records_to_send())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Upload new_videos.csv to the API, without scraping.")
    parser.add_argument("--dry-run", action="store_true", help="only count what would be sent")
    args = parser.parse_args(argv)
    yt.configure_logging()
    try:
        settings = yt.Settings.from_file(yt.CONFIG_FILE)
        if not settings.upload.enabled:
            raise yt.ConfigError("Uploads are switched off (upload.enabled is not true in config.json).")
        uploader = CsvUploader(yt.CsvVideoStore(yt.OUTPUT_CSV), yt.build_publisher(settings.upload),
                               yt.JsonFile(yt.PENDING_FILE))
        if args.dry_run:
            yt.log.info("Dry run: %d video(s) from %s would be sent, plus any in %s.",
                        len(uploader.records_to_send()), yt.OUTPUT_CSV.name, yt.PENDING_FILE.name)
            return 0
        report = uploader.run()
        yt.log.info("Done: %d sent, %d refused by the API (see %s).",
                    report.sent, report.rejected, yt.REJECTED_FILE.name)
        return 0
    except (yt.ConfigError, yt.PublishError) as e:
        yt.log.error("%s", e)
        return 1
    except Exception:
        yt.log.exception("Unexpected failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())