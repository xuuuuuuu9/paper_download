"""Orchestration: per-DOI mirror loop, challenge handling, and reporting."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .browser import solve_with_browser
from .cookies import CookieStore
from .download import download_pdf, save_bytes_as_pdf
from .fetch import MirrorSession
from .naming import doi_to_filename, doi_to_readable_pdf_path, doi_to_url
from .parse import extract_pdf_url, needs_verification

# Outcomes of a single mirror attempt.
_OK = "ok"        # downloaded — stop trying mirrors
_NEXT = "next"    # not here / failed — try the next mirror


@dataclass
class BatchResult:
    """Aggregate outcome of a full run."""

    ok: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class DownloadOutcome:
    """Outcome for one DOI download attempt."""

    status: str
    path: Path | None = None
    mirror: str = ""
    error: str = ""


class Downloader:
    def __init__(
        self,
        output_dir: Path,
        cookie_store: CookieStore,
        mirrors: tuple[str, ...] = config.MIRRORS,
        interactive: bool = True,
        delay: tuple[float, float] = (config.MIN_DELAY, config.MAX_DELAY),
        debug_dir: Path | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.store = cookie_store
        self.mirrors = mirrors
        self.interactive = interactive
        self.delay = delay
        self.debug_dir = Path(debug_dir) if debug_dir else None
        if self.debug_dir:
            self.debug_dir.mkdir(parents=True, exist_ok=True)

    # -- public API ---------------------------------------------------------

    def run(self, dois: list[str]) -> BatchResult:
        result = BatchResult()
        total = len(dois)
        for index, doi in enumerate(dois, start=1):
            print(f"\n[{index}/{total}] {doi}")
            outcome = self.download_one(doi)
            getattr(result, outcome.status).append(doi)
        return result

    def download_one(self, doi: str) -> DownloadOutcome:
        return self._process_doi(doi)

    # -- internals ----------------------------------------------------------

    def _process_doi(self, doi: str) -> DownloadOutcome:
        dest = doi_to_readable_pdf_path(self.output_dir, doi)
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  already exists, skip -> {dest.name}")
            return DownloadOutcome("skipped", path=dest, mirror="local")

        for mirror in self.mirrors:
            if self._try_mirror(doi, mirror, dest) == _OK:
                return DownloadOutcome("ok", path=dest, mirror=mirror)
        print("  all mirrors failed")
        return DownloadOutcome("failed", path=dest, error="all mirrors failed")

    def _try_mirror(self, doi: str, mirror: str, dest: Path) -> str:
        dest.parent.mkdir(parents=True, exist_ok=True)
        url = doi_to_url(mirror, doi)
        cookies = self.store.load(mirror)
        session = MirrorSession(mirror, cookies)

        self._sleep()
        try:
            resp = session.get(url)
        except Exception as exc:
            print(f"  [{mirror}] 请求失败: {exc}")
            return _NEXT
        self.store.save(mirror, session.cookies_dict())

        # Some mirrors serve the PDF straight from the DOI URL.
        content_type = (resp.headers.get("content-type") or "").lower()
        if "application/pdf" in content_type:
            ok, detail = save_bytes_as_pdf(resp.content, dest)
            if ok:
                print(f"  [{mirror}] ✓ 直接下载成功 ({detail}) → {dest.name}")
                return _OK
            print(f"  [{mirror}] 直链非 PDF: {detail}")
            return _NEXT

        html = resp.text
        base = str(resp.url)
        pdf_url = extract_pdf_url(html, base)

        # No PDF yet: is this a bot-gate we can clear, or genuinely not here?
        if pdf_url is None and needs_verification(html, resp.status_code):
            verified = self._verify(mirror, url, cookies)
            if verified is not None:
                session, html = verified
                base = url
                pdf_url = extract_pdf_url(html, base)

        if pdf_url is None:
            self._dump_debug(doi, mirror, html)
            print(f"  [{mirror}] 未找到 PDF 链接（此镜像无该文献，或验证未通过）")
            return _NEXT

        self._sleep()
        ok, detail = download_pdf(session, pdf_url, referer=url, dest=dest)
        self.store.save(mirror, session.cookies_dict())
        if ok:
            print(f"  [{mirror}] ✓ 下载成功 ({detail}) → {dest.name}")
            return _OK
        print(f"  [{mirror}] PDF 下载失败: {detail}")
        return _NEXT

    def _verify(
        self, mirror: str, url: str, cookies: dict[str, str]
    ) -> tuple[MirrorSession, str] | None:
        """Clear a challenge via the browser, then re-fetch.

        Returns ``(refreshed_session, new_html)`` on success, or ``None`` if
        skipped/failed so the caller falls through to the next mirror.
        """
        if not self.interactive:
            print(f"  [{mirror}] 检测到验证/拦截，已跳过（--no-interactive 模式）")
            return None

        print(f"  [{mirror}] 检测到验证/拦截，打开浏览器手动处理…")
        new_cookies = solve_with_browser(url, mirror, cookies)
        if not new_cookies:
            return None
        self.store.save(mirror, new_cookies)

        session = MirrorSession(mirror, new_cookies)
        self._sleep()
        try:
            resp = session.get(url)
        except Exception as exc:
            print(f"  [{mirror}] 验证后重试失败: {exc}")
            return None
        self.store.save(mirror, session.cookies_dict())
        return session, resp.text

    def _dump_debug(self, doi: str, mirror: str, html: str) -> None:
        if not self.debug_dir:
            return
        stem = doi_to_filename(doi)[:-4]  # drop ".pdf"
        path = self.debug_dir / f"{stem}__{mirror}.html"
        try:
            path.write_text(html, encoding="utf-8")
            print(f"  [{mirror}] 已保存调试页面: {path}")
        except OSError:
            pass

    def _sleep(self) -> None:
        low, high = self.delay
        time.sleep(random.uniform(low, high))
