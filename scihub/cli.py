"""Command-line interface for the local paper download pipeline."""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from . import config
from .cookies import CookieStore
from .metadata import OpenAlexClient
from .runner import Downloader
from .storage import ArticleRecord, Database


DEFAULT_DB = "data/papers.db"


def parse_journal_file(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Journal file not found: {path}")
    journals: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name = stripped
        url = ""
        if "\t" in stripped:
            name, url = [part.strip() for part in stripped.split("\t", 1)]
        elif " http://" in stripped or " https://" in stripped:
            parts = stripped.rsplit(None, 1)
            name = parts[0].strip()
            url = parts[1].strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            journals.append((name, url))
    return journals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local journal-to-PDF download pipeline")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create or upgrade the local SQLite database")

    import_cmd = sub.add_parser("import-journals", help="Import journals from name.txt")
    import_cmd.add_argument("-i", "--input", default="name.txt", help="Journal list file")

    discover = sub.add_parser("discover", help="Discover DOI records for imported journals")
    discover.add_argument("--limit-per-journal", type=int, default=100, help="Maximum DOI records per journal")
    discover.add_argument("--mailto", default="", help="Email passed to OpenAlex polite pool")

    download = sub.add_parser("download", help="Download queued DOI records from Sci-Hub")
    download.add_argument("--limit", type=int, default=50, help="Maximum jobs to process")
    download.add_argument("-o", "--output", default=config.DEFAULT_OUTPUT_DIR, help="PDF root directory")
    download.add_argument("--cookie-dir", default=config.DEFAULT_COOKIE_DIR)
    download.add_argument("--mirrors", nargs="+", default=list(config.MIRRORS))
    download.add_argument("--no-interactive", action="store_true")
    download.add_argument("--min-delay", type=float, default=config.MIN_DELAY)
    download.add_argument("--max-delay", type=float, default=config.MAX_DELAY)

    sub.add_parser("retry-failed", help="Move failed jobs back to pending")
    sub.add_parser("status", help="Show queue status counts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = Database(args.db)

    if args.command == "init-db":
        db.init()
        print(f"Database ready: {args.db}")
        return 0

    if args.command == "import-journals":
        db.init()
        journals = parse_journal_file(Path(args.input))
        imported = db.import_journals(journals)
        print(f"Imported journals: {imported} (from {args.input})")
        return 0

    if args.command == "discover":
        db.init()
        client = OpenAlexClient(mailto=args.mailto or None)
        total = 0
        for journal in db.list_journals():
            count = 0
            for article in client.iter_articles(journal["name"], limit=args.limit_per_journal):
                db.upsert_article(
                    ArticleRecord(
                        doi=article.doi,
                        journal_id=int(journal["id"]),
                        title=article.title,
                        year=article.year,
                        source=article.source,
                    )
                )
                count += 1
            total += count
            print(f"{journal['name']}: {count} DOI")
        print(f"Discovered DOI records: {total}")
        return 0

    if args.command == "download":
        db.init()
        downloader = Downloader(
            output_dir=Path(args.output),
            cookie_store=CookieStore(args.cookie_dir),
            mirrors=tuple(args.mirrors),
            interactive=not args.no_interactive,
            delay=(args.min_delay, args.max_delay),
        )
        jobs = db.get_jobs(limit=args.limit, statuses=("pending",))
        if not jobs:
            print("No pending jobs.")
            return 0
        for job in jobs:
            doi = str(job["doi"])
            outcome = downloader.download_one(doi)
            if outcome.status in ("ok", "skipped") and outcome.path and outcome.path.exists():
                db.mark_downloaded(
                    doi=doi,
                    path=outcome.path,
                    size_bytes=outcome.path.stat().st_size,
                    sha256=_sha256(outcome.path),
                    mirror=outcome.mirror,
                )
            else:
                db.mark_job_failed(doi, outcome.error or "download failed")
        return 0

    if args.command == "retry-failed":
        db.init()
        count = db.reset_failed_jobs()
        print(f"Retried jobs queued: {count}")
        return 0

    if args.command == "status":
        db.init()
        counts = db.status_counts()
        if not counts:
            print("No download jobs.")
            return 0
        for status, count in sorted(counts.items()):
            print(f"{status}: {count}")
        return 0

    return 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    sys.exit(main())
