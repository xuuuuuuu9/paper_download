"""Concurrent database-backed download scheduler."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .captcha import CaptchaCoordinator
from .cookies import CookieStore
from .runner import DownloadOutcome, Downloader
from .storage import Database

RETRYABLE = {"network_error", "http_error", "mirror_failed", "rate_limited", "captcha_required"}
TERMINAL = {"not_found", "invalid_pdf"}


@dataclass(frozen=True)
class SchedulerConfig:
    output_dir: Path
    cookie_dir: str
    mirrors: tuple[str, ...] = config.MIRRORS
    workers: int = 4
    per_mirror_workers: int = 1
    poll_seconds: float = 30.0
    stop_when_idle: bool = True
    max_attempts: int = 5
    retry_base_minutes: int = 30
    retry_max_hours: int = 24
    interactive_captcha: bool = True
    delay: tuple[float, float] = (config.MIN_DELAY, config.MAX_DELAY)
    max_jobs: int | None = None


class DownloadScheduler:
    def __init__(self, db: Database, cfg: SchedulerConfig, logger: logging.Logger | None = None) -> None:
        self.db = db
        self.cfg = cfg
        self.logger = logger or logging.getLogger(__name__)
        self.worker_id = f"download-{uuid.uuid4().hex[:8]}"
        self._processed = 0

    def run(self) -> int:
        self.db.recover_stale_leases(max_age_minutes=60)
        captcha = CaptchaCoordinator(interactive=self.cfg.interactive_captcha)
        mirror_semaphores = {
            mirror: threading.Semaphore(max(1, self.cfg.per_mirror_workers))
            for mirror in self.cfg.mirrors
        }
        downloader = Downloader(
            output_dir=self.cfg.output_dir,
            cookie_store=CookieStore(self.cfg.cookie_dir),
            mirrors=self.cfg.mirrors,
            interactive=self.cfg.interactive_captcha,
            delay=self.cfg.delay,
            captcha_coordinator=captcha,
            mirror_semaphores=mirror_semaphores,
        )

        futures: dict[Future[tuple[dict, DownloadOutcome]], dict] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.cfg.workers)) as pool:
            while True:
                self._submit_ready(pool, downloader, futures)
                if not futures:
                    if self.cfg.stop_when_idle:
                        break
                    self.logger.info("No ready jobs; sleeping %.1fs", self.cfg.poll_seconds)
                    time.sleep(self.cfg.poll_seconds)
                    continue

                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    job = futures.pop(future)
                    try:
                        _, outcome = future.result()
                    except Exception as exc:
                        outcome = DownloadOutcome("failed", error=str(exc), error_type="unknown_error")
                    self._record_outcome(job, outcome)
                    self._processed += 1
                if self.cfg.max_jobs is not None and self._processed >= self.cfg.max_jobs:
                    break
        return self._processed

    def _submit_ready(
        self,
        pool: ThreadPoolExecutor,
        downloader: Downloader,
        futures: dict[Future[tuple[dict, DownloadOutcome]], dict],
    ) -> None:
        remaining_slots = max(1, self.cfg.workers) - len(futures)
        if remaining_slots <= 0:
            return
        if self.cfg.max_jobs is not None:
            remaining_jobs = self.cfg.max_jobs - self._processed - len(futures)
            if remaining_jobs <= 0:
                return
            remaining_slots = min(remaining_slots, remaining_jobs)
        jobs = self.db.lease_jobs(self.worker_id, remaining_slots)
        for row in jobs:
            job = dict(row)
            self.logger.info("leased doi=%s job=%s", job["doi"], job["id"])
            future = pool.submit(self._download_job, downloader, job)
            futures[future] = job

    @staticmethod
    def _download_job(downloader: Downloader, job: dict) -> tuple[dict, DownloadOutcome]:
        return job, downloader.download_one(str(job["doi"]), job_id=job["id"])

    def _record_outcome(self, job: dict, outcome: DownloadOutcome) -> None:
        doi = str(job["doi"])
        self.logger.info(
            "finished doi=%s status=%s error_type=%s error=%s",
            doi,
            outcome.status,
            outcome.error_type,
            outcome.error,
        )
        if outcome.status in ("ok", "skipped") and outcome.path and outcome.path.exists():
            self.db.mark_downloaded(
                doi=doi,
                path=outcome.path,
                size_bytes=outcome.path.stat().st_size,
                sha256=_sha256(outcome.path),
                mirror=outcome.mirror,
            )
            return

        error_type = outcome.error_type or "unknown_error"
        error = outcome.error or "download failed"
        attempts = int(job["attempts"] or 0) + 1
        if error_type in TERMINAL or attempts >= self.cfg.max_attempts:
            self.db.mark_job_terminal_failed(doi, error_type, error)
            return
        if error_type in RETRYABLE:
            self.db.mark_job_retry_wait(doi, error_type, error, self._next_retry(error_type, attempts))
            return
        self.db.mark_job_terminal_failed(doi, error_type, error)

    def _next_retry(self, error_type: str, attempts: int) -> datetime:
        if error_type == "rate_limited":
            delay = timedelta(hours=6)
        else:
            minutes = min(
                self.cfg.retry_base_minutes * (2 ** max(0, attempts - 1)),
                self.cfg.retry_max_hours * 60,
            )
            delay = timedelta(minutes=minutes)
        return datetime.now(timezone.utc) + delay


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
