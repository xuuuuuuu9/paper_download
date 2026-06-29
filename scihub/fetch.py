"""HTTP layer: a mirror-bound, TLS-impersonating session.

A fresh session per mirror keeps cookie jars isolated by domain. curl_cffi's
``impersonate`` makes the TLS handshake look like real Chrome, which is what
gets past DDoS-Guard without a captcha most of the time.
"""
from __future__ import annotations

from . import config

try:
    from curl_cffi import requests as cffi_requests
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit(
        "缺少依赖 curl_cffi。请先安装：\n"
        "    pip install -r requirements.txt\n"
        f"(import error: {exc})"
    )


class MirrorSession:
    """A Chrome-impersonating HTTP session scoped to a single mirror domain."""

    def __init__(self, mirror: str, cookies: dict[str, str] | None = None) -> None:
        self.mirror = mirror
        self._session = cffi_requests.Session(impersonate=config.IMPERSONATE)
        for name, value in (cookies or {}).items():
            try:
                self._session.cookies.set(name, value, domain=mirror)
            except Exception:
                # A malformed stored cookie should never abort the run.
                continue

    def get(self, url: str, referer: str | None = None, stream: bool = False):
        headers = dict(config.BASE_HEADERS)
        if referer:
            headers["Referer"] = referer
            # When fetching the PDF object the browser sends these instead.
            headers["sec-fetch-dest"] = "object"
            headers["sec-fetch-site"] = "same-origin"
        return self._session.get(
            url,
            headers=headers,
            timeout=config.REQUEST_TIMEOUT,
            allow_redirects=True,
            stream=stream,
        )

    def cookies_dict(self) -> dict[str, str]:
        """Current cookie jar as a plain dict (for persistence)."""
        jar = self._session.cookies
        try:
            return dict(jar.get_dict())
        except AttributeError:
            pass
        try:
            return {cookie.name: cookie.value for cookie in jar.jar}
        except AttributeError:
            try:
                return dict(jar)
            except Exception:
                return {}
