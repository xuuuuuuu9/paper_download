"""SQLite storage for journals, discovered articles, download jobs, and PDFs."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Iterable


@dataclass(frozen=True)
class ArticleRecord:
    doi: str
    journal_id: int
    title: str = ""
    year: int | None = None
    source: str = ""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS journals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    source_url TEXT NOT NULL DEFAULT '',
                    issn TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doi TEXT NOT NULL UNIQUE,
                    journal_id INTEGER NOT NULL REFERENCES journals(id),
                    title TEXT NOT NULL DEFAULT '',
                    year INTEGER,
                    source TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS article_sources (
                    doi TEXT NOT NULL REFERENCES articles(doi),
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (doi, source)
                );

                CREATE TABLE IF NOT EXISTS download_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doi TEXT NOT NULL UNIQUE REFERENCES articles(doi),
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    last_error_type TEXT NOT NULL DEFAULT '',
                    next_retry_at TEXT,
                    leased_at TEXT,
                    lease_owner TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS pdf_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doi TEXT NOT NULL UNIQUE REFERENCES articles(doi),
                    status TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    mirror TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doi TEXT,
                    journal_id INTEGER,
                    kind TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            self._ensure_columns(conn)

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(download_jobs)").fetchall()}
        additions = {
            "last_error_type": "ALTER TABLE download_jobs ADD COLUMN last_error_type TEXT NOT NULL DEFAULT ''",
            "leased_at": "ALTER TABLE download_jobs ADD COLUMN leased_at TEXT",
            "lease_owner": "ALTER TABLE download_jobs ADD COLUMN lease_owner TEXT NOT NULL DEFAULT ''",
        }
        for column, sql in additions.items():
            if column not in existing:
                conn.execute(sql)

    def import_journals(self, journals: Iterable[tuple[str, str]]) -> int:
        imported = 0
        with self.connection() as conn:
            for raw_name, raw_url in journals:
                name = raw_name.strip()
                if not name:
                    continue
                before = conn.total_changes
                conn.execute(
                    "INSERT OR IGNORE INTO journals (name, source_url) VALUES (?, ?)",
                    (name, raw_url.strip()),
                )
                if conn.total_changes > before:
                    imported += 1
                    conn.execute(
                        "INSERT INTO events (journal_id, kind, message) VALUES (last_insert_rowid(), ?, ?)",
                        ("journal_imported", name),
                    )
        return imported

    def list_journals(self, status: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM journals"
        params: tuple[str, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY name"
        with self.connection() as conn:
            return list(conn.execute(sql, params))

    def upsert_article(self, article: ArticleRecord) -> int:
        doi = article.doi.strip().lower()
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO articles (doi, journal_id, title, year, source)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(doi) DO UPDATE SET
                    title = COALESCE(NULLIF(excluded.title, ''), articles.title),
                    year = COALESCE(excluded.year, articles.year),
                    source = COALESCE(NULLIF(excluded.source, ''), articles.source),
                    updated_at = CURRENT_TIMESTAMP
                """,
                (doi, article.journal_id, article.title, article.year, article.source),
            )
            conn.execute("INSERT OR IGNORE INTO download_jobs (doi, status) VALUES (?, 'pending')", (doi,))
            if article.source:
                conn.execute(
                    "INSERT OR IGNORE INTO article_sources (doi, source) VALUES (?, ?)",
                    (doi, article.source),
                )
            row = conn.execute("SELECT id FROM articles WHERE doi = ?", (doi,)).fetchone()
            return int(row["id"])

    def list_article_sources(self, doi: str) -> list[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT source FROM article_sources WHERE doi = ? ORDER BY source",
                (doi.strip().lower(),),
            ).fetchall()
            return [str(row["source"]) for row in rows]

    def mark_journal_status(self, journal_id: int, status: str, message: str = "") -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE journals
                SET status = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, journal_id),
            )
            kind = "journal_error" if status == "error" else "journal_status"
            conn.execute(
                "INSERT INTO events (journal_id, kind, message) VALUES (?, ?, ?)",
                (journal_id, kind, message),
            )

    def list_events(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            )

    def lease_jobs(self, worker_id: str, limit: int) -> list[sqlite3.Row]:
        now = _utc_now()
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT download_jobs.*, articles.title, articles.journal_id
                FROM download_jobs
                JOIN articles ON articles.doi = download_jobs.doi
                WHERE download_jobs.status = 'pending'
                   OR (download_jobs.status = 'retry_wait'
                       AND (download_jobs.next_retry_at IS NULL OR download_jobs.next_retry_at <= ?))
                ORDER BY download_jobs.created_at, download_jobs.id
                LIMIT ?
                """,
                (now, limit),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE download_jobs
                    SET status = 'leased',
                        leased_at = ?,
                        lease_owner = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    (now, worker_id, *ids),
                )
                rows = conn.execute(
                    f"""
                    SELECT download_jobs.*, articles.title, articles.journal_id
                    FROM download_jobs
                    JOIN articles ON articles.doi = download_jobs.doi
                    WHERE download_jobs.id IN ({placeholders})
                    ORDER BY download_jobs.id
                    """,
                    ids,
                ).fetchall()
            return list(rows)

    def get_jobs(
        self,
        limit: int,
        statuses: tuple[str, ...] = ("pending", "failed", "downloaded", "skipped"),
    ) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in statuses)
        with self.connection() as conn:
            return list(
                conn.execute(
                    f"""
                    SELECT download_jobs.*, articles.title, articles.journal_id
                    FROM download_jobs
                    JOIN articles ON articles.doi = download_jobs.doi
                    WHERE download_jobs.status IN ({placeholders})
                    ORDER BY download_jobs.created_at, download_jobs.id
                    LIMIT ?
                    """,
                    (*statuses, limit),
                )
            )

    def mark_job_failed(self, doi: str, error: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE download_jobs
                SET status = 'failed',
                    attempts = attempts + 1,
                    last_error = ?,
                    last_error_type = 'unknown_error',
                    leased_at = NULL,
                    lease_owner = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE doi = ?
                """,
                (error, doi),
            )
            conn.execute("INSERT INTO events (doi, kind, message) VALUES (?, 'download_failed', ?)", (doi, error))

    def mark_job_retry_wait(self, doi: str, error_type: str, error: str, next_retry_at: datetime) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE download_jobs
                SET status = 'retry_wait',
                    attempts = attempts + 1,
                    last_error_type = ?,
                    last_error = ?,
                    next_retry_at = ?,
                    leased_at = NULL,
                    lease_owner = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE doi = ?
                """,
                (error_type, error, _format_dt(next_retry_at), doi),
            )
            conn.execute(
                "INSERT INTO events (doi, kind, message) VALUES (?, 'download_retry_wait', ?)",
                (doi, f"{error_type}: {error}"),
            )

    def mark_job_terminal_failed(self, doi: str, error_type: str, error: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE download_jobs
                SET status = 'failed',
                    attempts = attempts + 1,
                    last_error_type = ?,
                    last_error = ?,
                    next_retry_at = NULL,
                    leased_at = NULL,
                    lease_owner = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE doi = ?
                """,
                (error_type, error, doi),
            )
            conn.execute(
                "INSERT INTO events (doi, kind, message) VALUES (?, 'download_failed', ?)",
                (doi, f"{error_type}: {error}"),
            )

    def recover_stale_leases(self, max_age_minutes: int) -> int:
        cutoff = _format_dt(datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes))
        with self.connection() as conn:
            before = conn.total_changes
            conn.execute(
                """
                UPDATE download_jobs
                SET status = 'pending',
                    leased_at = NULL,
                    lease_owner = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'leased'
                  AND (leased_at IS NULL OR leased_at <= ?)
                """,
                (cutoff,),
            )
            return conn.total_changes - before

    def reset_failed_jobs(self, error_type: str | None = None) -> int:
        with self.connection() as conn:
            before = conn.total_changes
            params: tuple[str, ...] = ()
            where = "status = 'failed'"
            if error_type:
                where += " AND last_error_type = ?"
                params = (error_type,)
            conn.execute(
                f"""
                UPDATE download_jobs
                SET status = 'pending',
                    last_error = '',
                    last_error_type = '',
                    next_retry_at = NULL,
                    leased_at = NULL,
                    lease_owner = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE {where}
                """,
                params,
            )
            return conn.total_changes - before

    def mark_downloaded(self, doi: str, path: Path, size_bytes: int, sha256: str, mirror: str) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                INSERT INTO pdf_files (doi, status, file_path, size_bytes, sha256, mirror)
                VALUES (?, 'downloaded', ?, ?, ?, ?)
                ON CONFLICT(doi) DO UPDATE SET
                    status = 'downloaded',
                    file_path = excluded.file_path,
                    size_bytes = excluded.size_bytes,
                    sha256 = excluded.sha256,
                    mirror = excluded.mirror,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (doi, path.as_posix(), size_bytes, sha256, mirror),
            )
            conn.execute(
                """
                UPDATE download_jobs
                SET status = 'downloaded',
                    last_error = '',
                    last_error_type = '',
                    next_retry_at = NULL,
                    leased_at = NULL,
                    lease_owner = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE doi = ?
                """,
                (doi,),
            )
            conn.execute("INSERT INTO events (doi, kind, message) VALUES (?, 'downloaded', ?)", (doi, path.as_posix()))

    def status_counts(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM download_jobs GROUP BY status").fetchall()
            return {str(row["status"]): int(row["count"]) for row in rows}

    def error_counts(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT last_error_type, COUNT(*) AS count
                FROM download_jobs
                WHERE last_error_type != ''
                GROUP BY last_error_type
                ORDER BY last_error_type
                """
            ).fetchall()
            return {str(row["last_error_type"]): int(row["count"]) for row in rows}

    def failed_jobs(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    SELECT download_jobs.*, articles.title, journals.name AS journal
                    FROM download_jobs
                    JOIN articles ON articles.doi = download_jobs.doi
                    JOIN journals ON journals.id = articles.journal_id
                    WHERE download_jobs.status IN ('failed', 'retry_wait')
                    ORDER BY download_jobs.updated_at DESC
                    """
                )
            )

    def downloaded_files(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return list(
                conn.execute(
                    """
                    SELECT pdf_files.*, articles.title, journals.name AS journal
                    FROM pdf_files
                    JOIN articles ON articles.doi = pdf_files.doi
                    JOIN journals ON journals.id = articles.journal_id
                    ORDER BY pdf_files.updated_at DESC
                    """
                )
            )


def _utc_now() -> str:
    return _format_dt(datetime.now(timezone.utc))


def _format_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")
