"""Command-line interface for the local paper download pipeline."""
from __future__ import annotations

import argparse
import csv
import hashlib
import logging
import sys
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from . import config
from .cookies import CookieStore
from .metadata import CrossrefClient, OpenAlexClient
from .runner import Downloader
from .scheduler import DownloadScheduler, SchedulerConfig
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
    max_jobs: int | None = None
    download_workers: int = 4
    per_mirror_workers: int = 1
    download_poll_seconds: float = 30.0
    stop_when_idle: bool = True
    max_attempts: int = 5
    retry_base_minutes: int = 30
    retry_max_hours: int = 24
    interactive_captcha: bool = True
    log_dir: str = "logs"
    from_year: int | None = None
    to_year: int | None = None
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
    discover.add_argument("--from-year", type=int, default=None, help="Oldest publication year to discover")
    discover.add_argument("--to-year", type=int, default=None, help="Newest publication year to discover")

    download = sub.add_parser("download", help="Download queued DOI records from Sci-Hub")
    download.add_argument("--limit", type=int, default=None, help=argparse.SUPPRESS)
    download.add_argument("--max-jobs", type=int, default=None, help="Debug/test cap for total jobs processed")
    download.add_argument("--workers", type=int, default=None, help="Global concurrent download workers")
    download.add_argument("--per-mirror-workers", type=int, default=None, help="Concurrent workers per mirror")
    download.add_argument("-o", "--output", default=None, help="PDF root directory")
    download.add_argument("--cookie-dir", default=None)
    download.add_argument("--mirrors", nargs="+", default=None)
    download.add_argument("--no-interactive", action="store_true", default=None)
    download.add_argument("--min-delay", type=float, default=None)
    download.add_argument("--max-delay", type=float, default=None)

    retry = sub.add_parser("retry-failed", help="Move failed jobs back to pending")
    retry.add_argument("--error-type", default=None)
    retry.add_argument("--all", action="store_true")
    sub.add_parser("status", help="Show queue status counts")
    events = sub.add_parser("recent-events", help="Show recent event records")
    events.add_argument("--limit", type=int, default=50)
    failed = sub.add_parser("export-failed", help="Export failed/retry-wait jobs as CSV")
    failed.add_argument("-o", "--output", required=True)
    downloaded = sub.add_parser("export-downloaded", help="Export downloaded PDFs as CSV")
    downloaded.add_argument("-o", "--output", required=True)
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
        max_jobs=getattr(args, "max_jobs", None),
        download_workers=getattr(args, "workers", None)
        or _env_int(env, "PAPER_DOWNLOAD_WORKERS", defaults.download_workers),
        per_mirror_workers=getattr(args, "per_mirror_workers", None)
        or _env_int(env, "PAPER_PER_MIRROR_WORKERS", defaults.per_mirror_workers),
        download_poll_seconds=_env_float(env, "PAPER_DOWNLOAD_POLL_SECONDS", defaults.download_poll_seconds),
        stop_when_idle=_env_bool(env, "PAPER_STOP_WHEN_IDLE", defaults.stop_when_idle),
        max_attempts=_env_int(env, "PAPER_MAX_ATTEMPTS", defaults.max_attempts),
        retry_base_minutes=_env_int(env, "PAPER_RETRY_BASE_MINUTES", defaults.retry_base_minutes),
        retry_max_hours=_env_int(env, "PAPER_RETRY_MAX_HOURS", defaults.retry_max_hours),
        interactive_captcha=_env_bool(env, "PAPER_INTERACTIVE_CAPTCHA", defaults.interactive_captcha),
        log_dir=_env_str(env, "PAPER_LOG_DIR", defaults.log_dir),
        from_year=getattr(args, "from_year", None) or _env_optional_int(env, "PAPER_FROM_YEAR"),
        to_year=getattr(args, "to_year", None) or _env_optional_int(env, "PAPER_TO_YEAR"),
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


def _env_optional_int(env: dict, key: str) -> int | None:
    value = env.get(key)
    return int(value) if value is not None and str(value).strip() else None


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
                try:
                    for article in client.iter_articles(
                        journal["name"],
                        limit=settings.limit_per_journal,
                        from_year=settings.from_year,
                        to_year=settings.to_year,
                    ):
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
                except Exception as exc:
                    message = f"{client.__class__.__name__}: {exc}"
                    db.mark_journal_status(int(journal["id"]), "error", message)
                    print(f"{journal['name']} [{client.__class__.__name__}] failed: {exc}")
            total += count
            if count:
                db.mark_journal_status(int(journal["id"]), "discovered", f"{count} DOI candidates")
            print(f"{journal['name']}: {count} DOI candidates")
        print(f"Discovered DOI records: {total}")
        return 0

    if args.command == "download":
        db.init()
        logger = _setup_download_logger(Path(settings.log_dir))
        scheduler = DownloadScheduler(
            db=db,
            cfg=SchedulerConfig(
                output_dir=Path(settings.output_dir),
                cookie_dir=settings.cookie_dir,
                mirrors=tuple(settings.mirrors or list(config.MIRRORS)),
                workers=settings.download_workers,
                per_mirror_workers=settings.per_mirror_workers,
                poll_seconds=settings.download_poll_seconds,
                stop_when_idle=settings.stop_when_idle,
                max_attempts=settings.max_attempts,
                retry_base_minutes=settings.retry_base_minutes,
                retry_max_hours=settings.retry_max_hours,
                interactive_captcha=settings.interactive_captcha and not settings.no_interactive,
                delay=(settings.min_delay, settings.max_delay),
                max_jobs=settings.max_jobs if settings.max_jobs is not None else getattr(args, "limit", None),
            ),
            logger=logger,
        )
        processed = scheduler.run()
        print(f"Processed jobs: {processed}")
        return 0

    if args.command == "retry-failed":
        db.init()
        count = db.reset_failed_jobs(error_type=args.error_type)
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
        errors = db.error_counts()
        for error_type, count in sorted(errors.items()):
            print(f"error.{error_type}: {count}")
        return 0

    if args.command == "recent-events":
        db.init()
        for row in db.list_events(limit=args.limit):
            print(f"{row['created_at']}\t{row['kind']}\t{row['doi'] or row['journal_id'] or ''}\t{row['message']}")
        return 0

    if args.command == "export-failed":
        db.init()
        _write_csv(
            Path(args.output),
            ["doi", "title", "journal", "status", "last_error_type", "last_error", "attempts", "next_retry_at"],
            db.failed_jobs(),
        )
        print(f"Exported failed jobs: {args.output}")
        return 0

    if args.command == "export-downloaded":
        db.init()
        _write_csv(
            Path(args.output),
            ["doi", "title", "journal", "file_path", "size_bytes", "sha256", "mirror", "created_at"],
            db.downloaded_files(),
        )
        print(f"Exported downloaded files: {args.output}")
        return 0

    return 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _setup_download_logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"download-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    logger = logging.getLogger("scihub.download")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    logger.info("download log: %s", path)
    return logger


def _write_csv(path: Path, fieldnames: list[str], rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True) if path.parent != Path(".") else None
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row[field] if field in row.keys() else "" for field in fieldnames})


if __name__ == "__main__":
    sys.exit(main())
