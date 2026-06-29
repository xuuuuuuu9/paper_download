"""HTML inspection: locate the PDF link and recognise challenge pages."""
from __future__ import annotations

import re
from urllib.parse import urljoin

# The canonical source of the PDF on a Sci-Hub detail page, e.g.:
#   <meta name="citation_pdf_url" content="/storage/.../itagaki2006.pdf">
# Two patterns cover both attribute orderings.
_META_PATTERNS = (
    re.compile(
        r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]+content=["\']([^"\']+)["\']',
        re.IGNORECASE,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']citation_pdf_url["\']',
        re.IGNORECASE,
    ),
)

# Fallbacks if the meta tag is missing but the embedded viewer/link is present.
_OBJECT_PATTERN = re.compile(
    r'<object[^>]+data=["\']([^"\']+\.pdf[^"\']*)["\']', re.IGNORECASE
)
_DOWNLOAD_LINK_PATTERN = re.compile(
    r'href=["\'](/?[^"\']*?\.pdf[^"\']*)["\']', re.IGNORECASE
)

# Markers that indicate a bot-check / challenge rather than a real result.
_CHALLENGE_MARKERS = (
    "checking your browser",
    "just a moment",
    "<title>ddos-guard",
    "challenge-platform",
    "cf-browser-verification",
    "verifying you are human",
    "attention required",
)

# DDoS-Guard interstitial markers. These appear in the *body* only on the
# JS-challenge page (which sets __ddg* cookies via inline script); a real
# article page references DDoS-Guard only through an external script while also
# carrying citation_pdf_url, so this branch is never reached for it.
_DDOS_MARKERS = (
    "/.well-known/ddos-guard",
    "ddos-guard",
    "__ddg",
)

# Sci-Hub's own captcha gate (shown instead of the PDF when it suspects a bot).
_CAPTCHA_MARKERS = (
    'id="captcha"',
    'name="answer"',
    "captcha-image",
    "请输入验证码",
    "введите капчу",
    "enter the captcha",
)

# A real article page is large; interstitials/redirects are tiny.
_SHORT_PAGE_BYTES = 1500

_PDF_FRAGMENT_STRIP = re.compile(r"#.*$")


def extract_pdf_url(html: str, page_url: str) -> str | None:
    """Return an absolute PDF URL from a detail page, or ``None`` if absent."""
    for pattern in _META_PATTERNS:
        match = pattern.search(html)
        if match:
            return _absolutise(match.group(1), page_url)

    match = _OBJECT_PATTERN.search(html)
    if match:
        return _absolutise(match.group(1), page_url)

    match = _DOWNLOAD_LINK_PATTERN.search(html)
    if match:
        return _absolutise(match.group(1), page_url)

    return None


def _absolutise(candidate: str, page_url: str) -> str:
    cleaned = _PDF_FRAGMENT_STRIP.sub("", candidate.strip())
    return urljoin(page_url, cleaned)


def needs_verification(html: str, status_code: int) -> bool:
    """Decide, *when no PDF link was found*, whether the page is a bot-gate
    (DDoS-Guard / captcha / block) that we should clear manually, rather than a
    genuine "article not available on this mirror" result.

    This must only be called after ``extract_pdf_url`` has returned ``None``, so
    a successful page (which always carries ``citation_pdf_url``) never reaches
    here and the DDoS-Guard markers below are safe to use.
    """
    if status_code in (401, 403, 429, 503):
        return True
    low = html.lower()
    if any(marker in low for marker in _CHALLENGE_MARKERS):
        return True
    if any(marker in low for marker in _DDOS_MARKERS):
        return True
    if any(marker in low for marker in _CAPTCHA_MARKERS):
        return True
    # Tiny bodies are JS interstitials / meta-refresh redirects, not articles.
    if len(html.strip()) < _SHORT_PAGE_BYTES:
        return True
    return False
