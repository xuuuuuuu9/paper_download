"""Sci-Hub batch downloader.

Reads DOIs line by line and downloads the matching PDF from the first working
mirror, with TLS-impersonation to avoid DDoS-Guard challenges and a manual
browser fallback when a challenge cannot be avoided.
"""

__all__ = [
    "browser",
    "captcha",
    "cli",
    "config",
    "cookies",
    "download",
    "fetch",
    "metadata",
    "naming",
    "parse",
    "runner",
    "scheduler",
    "storage",
]
