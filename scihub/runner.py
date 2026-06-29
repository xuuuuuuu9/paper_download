"""Orchestration for DOI downloads across Sci-Hub mirrors."""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .browser import solve_with_browser
from .captcha import CaptchaCoordinator
from .cookies import CookieStore
from .download import download_pdf, save_bytes_as_pdf
from .fetch import MirrorSession
from .naming import doi_to_filename, doi_to_readable_pdf_path, doi_to_url
from .parse import extract_pdf_url, needs_verification

_OK = "ok"
_NEXT = "next"


@dataclass
class BatchResult:
    ok: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class DownloadOutcome:
    status: str
    path: Path | None = None
    mirror: str = ""
    error: str = ""
    error_type: str = ""


class Downloader:
    def __init__(
        self,
        output_dir: Path,
        cookie_store: CookieStore,
        mirrors: tuple[str, ...] = config.MIRRORS,
        interactive: bool = True,
        delay: tuple[float, float] = (config.MIN_DELAY, config.MAX_DELAY),
        debug_dir: Path | None = None,
        captcha_coordinator: CaptchaCoordinator | None = None,
        mirror_semaphores: dict[str, threading.Semaphore] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.store = cookie_store
        self.mirrors = mirrors
        self.interactive = interactive
        self.delay = delay
        self.debug_dir = Path(debug_dir) if debug_dir else None
        self.captcha = captcha_coordinator
        self.mirror_semaphores = mirror_semaphores or {}
        if self.debug_dir:
            self.debug_dir.mkdir(parents=True, exist_ok=True)

    def run(self, dois: list[str]) -> BatchResult:
        result = BatchResult()
        total = len(dois)
        for index, doi in enumerate(dois, start=1):
            print(f"\n[{index}/{total}] {doi}")
            outcome = self.download_one(doi)
            getattr(result, outcome.status).append(doi)
        return result

    def download_one(self, doi: str, job_id: int | str | None = None) -> DownloadOutcome:
        return self._process_doi(doi, job_id=job_id)

    def _process_doi(self, doi: str, job_id: int | str | None = None) -> DownloadOutcome:
        dest = doi_to_readable_pdf_path(self.output_dir, doi)
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  already exists, skip -> {dest.name}")
            return DownloadOutcome("skipped", path=dest, mirror="local")

        last_error = "all mirrors failed"
        last_error_type = "mirror_failed"
        for mirror in self.mirrors:
            status, error_type, error = self._try_mirror(doi, mirror, dest, job_id=job_id)
            if status == _OK:
                return DownloadOutcome("ok", path=dest, mirror=mirror)
            last_error = error or last_error
            last_error_type = error_type or last_error_type

        print("  all mirrors failed")
        return DownloadOutcome("failed", path=dest, error=last_error, error_type=last_error_type)

    def _try_mirror(
        self,
        doi: str,
        mirror: str,
        dest: Path,
        job_id: int | str | None = None,
    ) -> tuple[str, str, str]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        semaphore = self.mirror_semaphores.get(mirror)
        if semaphore:
            semaphore.acquire()
        try:
            return self._try_mirror_locked(doi, mirror, dest, job_id=job_id)
        finally:
            if semaphore:
                semaphore.release()

    def _try_mirror_locked(
        self,
        doi: str,
        mirror: str,
        dest: Path,
        job_id: int | str | None = None,
    ) -> tuple[str, str, str]:
        url = doi_to_url(mirror, doi)
        cookies = self.store.load(mirror)
        session = MirrorSession(mirror, cookies)
        temp_suffix = f".{job_id}" if job_id is not None else ""

        self._sleep()
        try:
            resp = session.get(url)
        except Exception as exc:
            error = f"request failed: {exc}"
            print(f"  [{mirror}] {error}")
            return _NEXT, "network_error", error
        self.store.save(mirror, session.cookies_dict())

        content_type = (resp.headers.get("content-type") or "").lower()
        if "application/pdf" in content_type:
            ok, detail = save_bytes_as_pdf(resp.content, dest, temp_suffix=temp_suffix)
            if ok:
                print(f"  [{mirror}] direct PDF saved ({detail}) -> {dest.name}")
                return _OK, "", ""
            return _NEXT, classify_error(detail), detail

        html = resp.text
        base = str(resp.url)
        pdf_url = extract_pdf_url(html, base)
        verification_page = pdf_url is None and needs_verification(html, resp.status_code)

        if verification_page:
            verified = self._verify(mirror, url, cookies)
            if verified is not None:
                session, html = verified
                base = url
                pdf_url = extract_pdf_url(html, base)

        if pdf_url is None:
            self._dump_debug(doi, mirror, html)
            if verification_page:
                return _NEXT, "captcha_required", "captcha required"
            return _NEXT, "not_found", "PDF link not found"

        self._sleep()
        ok, detail = download_pdf(session, pdf_url, referer=url, dest=dest, temp_suffix=temp_suffix)
        self.store.save(mirror, session.cookies_dict())
        if ok:
            print(f"  [{mirror}] downloaded ({detail}) -> {dest.name}")
            return _OK, "", ""
        return _NEXT, classify_error(detail), detail

    def _verify(
        self,
        mirror: str,
        url: str,
        cookies: dict[str, str],
    ) -> tuple[MirrorSession, str] | None:
        if not self.interactive:
            print(f"  [{mirror}] captcha/challenge detected, skipped in non-interactive mode")
            return None

        print(f"  [{mirror}] captcha/challenge detected, opening browser")
        new_cookies = (
            self.captcha.solve(mirror, url, cookies)
            if self.captcha
            else solve_with_browser(url, mirror, cookies)
        )
        if not new_cookies:
            return None
        self.store.save(mirror, new_cookies)

        session = MirrorSession(mirror, new_cookies)
        self._sleep()
        try:
            resp = session.get(url)
        except Exception as exc:
            print(f"  [{mirror}] retry after captcha failed: {exc}")
            return None
        self.store.save(mirror, session.cookies_dict())
        return session, resp.text

    def _dump_debug(self, doi: str, mirror: str, html: str) -> None:
        if not self.debug_dir:
            return
        stem = doi_to_filename(doi)[:-4]
        path = self.debug_dir / f"{stem}__{mirror}.html"
        try:
            path.write_text(html, encoding="utf-8")
            print(f"  [{mirror}] debug page saved: {path}")
        except OSError:
            pass

    def _sleep(self) -> None:
        low, high = self.delay
        time.sleep(random.uniform(low, high))


def classify_error(detail: str) -> str:
    low = detail.lower()
    if "429" in low:
        return "rate_limited"
    if "captcha" in low or "challenge" in low or "verification" in low or "验证" in detail:
        return "captcha_required"
    if "not a pdf" in low or "不是 pdf" in detail or "闈?pdf" in low:
        return "invalid_pdf"
    if "request" in low or "timeout" in low or "connection" in low or "请求" in detail:
        return "network_error"
    if "all mirrors failed" in low:
        return "mirror_failed"
    if "http" in low:
        return "http_error"
    return "unknown_error"
