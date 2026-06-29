"""Command-line interface for the local paper download pipeline."""
from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from . import config
from .cookies import CookieStore
from .metadata import CrossrefClient, OpenAlexClient
from .runner import Downloader
from .storage import ArticleRecord, Database


DEFAULT_DB = "data/papers.db"


@dataclass(frozen=True)
class Settings:
    db: str = DEFAULT_DB
    journal_file: str = "name.txt"
    output_dir: str = config.DEFAULT_OUTPUT_DIR
    cookie_dir: str = config.DEFAULT_COOKIE_DIR
    mailto: str = ""
    limit_per_journal: int = 100
    download_limit: int = 50
    min_delay: float = config.MIN_DELAY
    max_delay: float = config.MAX_DELAY
    no_interactive: bool = False
    mirrors: list[str] | None = None


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
    parser.add_argument("--env-file", default=".env", help="Environment config file (default: .env)")
    parser.add_argument("--db", default=None, help=f"SQLite database path (default: {DEFAULT_DB})")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create or upgrade the local SQLite database")

    import_cmd = sub.add_parser("import-journals", help="Import journals from name.txt")
    import_cmd.add_argument("-i", "--input", default=None, help="Journal list file")

    discover = sub.add_parser("discover", help="Discover DOI records for imported journals")
    discover.add_argument("--limit-per-journal", type=int, default=None, help="Maximum DOI records per journal")
    discover.add_argument("--mailto", default=None, help="Email passed to OpenAlex polite pool")

    download = sub.add_parser("download", help="Download queued DOI records from Sci-Hub")
    download.add_argument("--limit", type=int, default=None, help="Maximum jobs to process")
    download.add_argument("-o", "--output", default=None, help="PDF root directory")
    download.add_argument("--cookie-dir", default=None)
    download.add_argument("--mirrors", nargs="+", default=None)
    download.add_argument("--no-interactive", action="store_true", default=None)
    download.add_argument("--min-delay", type=float, default=None)
    download.add_argument("--max-delay", type=float, default=None)

    sub.add_parser("retry-failed", help="Move failed jobs back to pending")
    sub.add_parser("status", help="Show queue status counts")
    return parser


def load_settings(args: argparse.Namespace) -> Settings:
    env = _clean_env(dotenv_values(args.env_file)) if args.env_file else {}
    defaults = Settings(mirrors=list(config.MIRRORS))

    return Settings(
        db=args.db or _env_str(env, "PAPER_DB", defaults.db),
        journal_file=getattr(args, "input", None) or _env_str(env, "PAPER_JOURNAL_FILE", defaults.journal_file),
        output_dir=getattr(args, "output", None) or _env_str(env, "PAPER_OUTPUT_DIR", defaults.output_dir),
        cookie_dir=getattr(args, "cookie_dir", None) or _env_str(env, "PAPER_COOKIE_DIR", defaults.cookie_dir),
        mailto=getattr(args, "mailto", None) or _env_str(env, "PAPER_MAILTO", defaults.mailto),
        limit_per_journal=getattr(args, "limit_per_journal", None)
        or _env_int(env, "PAPER_LIMIT_PER_JOURNAL", defaults.limit_per_journal),
        download_limit=getattr(args, "limit", None) or _env_int(env, "PAPER_DOWNLOAD_LIMIT", defaults.download_limit),
        min_delay=getattr(args, "min_delay", None) or _env_float(env, "PAPER_MIN_DELAY", defaults.min_delay),
        max_delay=getattr(args, "max_delay", None) or _env_float(env, "PAPER_MAX_DELAY", defaults.max_delay),
        no_interactive=(
            getattr(args, "no_interactive", None)
            if getattr(args, "no_interactive", None) is not None
            else _env_bool(env, "PAPER_NO_INTERACTIVE", defaults.no_interactive)
        ),
        mirrors=getattr(args, "mirrors", None) or _env_list(env, "PAPER_MIRRORS", defaults.mirrors or []),
    )


def _env_str(env: dict, key: str, default: str) -> str:
    value = env.get(key)
    return str(value).strip() if value is not None and str(value).strip() else default


def _env_int(env: dict, key: str, default: int) -> int:
    value = env.get(key)
    return int(value) if value is not None and str(value).strip() else default


def _env_float(env: dict, key: str, default: float) -> float:
    value = env.get(key)
    return float(value) if value is not None and str(value).strip() else default


def _env_bool(env: dict, key: str, default: bool) -> bool:
    value = env.get(key)
    if value is None or not str(value).strip():
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_list(env: dict, key: str, default: list[str]) -> list[str]:
    value = env.get(key)
    if value is None or not str(value).strip():
        return list(default)
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _clean_env(env: dict) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for key, value in env.items():
        if key is None or value is None:
            continue
        cleaned[str(key).lstrip("\ufeff").strip()] = str(value)
    return cleaned


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args)
    db = Database(settings.db)

    if args.command == "init-db":
        db.init()
        print(f"Database ready: {settings.db}")
        return 0

    if args.command == "import-journals":
        db.init()
        journals = parse_journal_file(Path(settings.journal_file))
        imported = db.import_journals(journals)
        print(f"Imported journals: {imported} (from {settings.journal_file})")
        return 0

    if args.command == "discover":
        db.init()
        clients = [
            OpenAlexClient(mailto=settings.mailto or None),
            CrossrefClient(mailto=settings.mailto or None),
        ]
        total = 0
        for journal in db.list_journals():
            count = 0
            for client in clients:
                source_count = 0
                for article in client.iter_articles(journal["name"], limit=settings.limit_per_journal):
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
                    source_count += 1
                print(f"{journal['name']} [{client.__class__.__name__}]: {source_count} DOI")
            total += count
            print(f"{journal['name']}: {count} DOI candidates")
        print(f"Discovered DOI records: {total}")
        return 0

    if args.command == "download":
        db.init()
        downloader = Downloader(
            output_dir=Path(settings.output_dir),
            cookie_store=CookieStore(settings.cookie_dir),
            mirrors=tuple(settings.mirrors or list(config.MIRRORS)),
            interactive=not settings.no_interactive,
            delay=(settings.min_delay, settings.max_delay),
        )
        jobs = db.get_jobs(limit=settings.download_limit, statuses=("pending",))
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
