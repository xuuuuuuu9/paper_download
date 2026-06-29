"""Static configuration: mirror list, headers, and tunable limits.

Keep behaviour-affecting constants here so the rest of the code stays declarative.
"""
from __future__ import annotations

# Mirrors are tried in order until one yields the paper. Order matters: earlier
# = preferred. Adjust freely if a mirror goes down or a faster one appears.
MIRRORS: tuple[str, ...] = (
    "sci-hub.ru",
    "sci-hub.st",
    "sci-hub.su",
    "sci-hub.box",
)

# Browser-like headers copied from the sample request. curl_cffi additionally
# matches Chrome's TLS/JA3 fingerprint, which is what actually defeats the
# DDoS-Guard JA3 check — headers alone are not enough.
BASE_HEADERS: dict[str, str] = {
    "accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "accept-language": "zh-CN,zh;q=0.9",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

# curl_cffi browser profile used for impersonation.
IMPERSONATE = "chrome"

# Network + politeness tuning.
REQUEST_TIMEOUT = 45  # seconds, per request
MIN_DELAY = 2.0       # min seconds between requests (anti-rate-limit)
MAX_DELAY = 5.0       # max seconds between requests

# A valid PDF starts with this signature; used to reject HTML challenge pages
# that masquerade as a download.
PDF_MAGIC = b"%PDF"

# Default on-disk locations (overridable via CLI).
DEFAULT_INPUT = "links.txt"
DEFAULT_OUTPUT_DIR = "papers"
DEFAULT_COOKIE_DIR = ".cookies"
