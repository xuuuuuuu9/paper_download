"""SQLite storage for journals, discovered articles, download jobs, and PDFs."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
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

                CREATE TABLE IF NOT EXISTS download_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doi TEXT NOT NULL UNIQUE REFERENCES articles(doi),
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    next_retry_at TEXT,
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
            row = conn.execute("SELECT id FROM articles WHERE doi = ?", (doi,)).fetchone()
            return int(row["id"])

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
                    updated_at = CURRENT_TIMESTAMP
                WHERE doi = ?
                """,
                (error, doi),
            )
            conn.execute("INSERT INTO events (doi, kind, message) VALUES (?, 'download_failed', ?)", (doi, error))

    def reset_failed_jobs(self) -> int:
        with self.connection() as conn:
            before = conn.total_changes
            conn.execute(
                """
                UPDATE download_jobs
                SET status = 'pending',
                    last_error = '',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'failed'
                """
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
