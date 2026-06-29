"""Serialized captcha solving for concurrent download workers."""
from __future__ import annotations

import threading
from collections.abc import Callable

from .browser import solve_with_browser

Solver = Callable[[str, str, dict[str, str]], dict[str, str]]


class CaptchaCoordinator:
    """Ensure only one browser challenge is active per mirror at a time."""

    def __init__(self, interactive: bool = True, solver: Solver = solve_with_browser) -> None:
        self.interactive = interactive
        self._solver = solver
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._results: dict[str, dict[str, str]] = {}

    def solve(self, mirror: str, url: str, cookies: dict[str, str]) -> dict[str, str]:
        if not self.interactive:
            return {}

        with self._lock:
            cached = self._results.get(mirror)
            if cached is not None:
                return dict(cached)
            event = self._inflight.get(mirror)
            if event is None:
                event = threading.Event()
                self._inflight[mirror] = event
                owner = True
            else:
                owner = False

        if owner:
            try:
                result = self._solver(url, mirror, cookies) or {}
                with self._lock:
                    self._results[mirror] = dict(result)
                    return dict(result)
            finally:
                with self._lock:
                    self._inflight.pop(mirror, None)
                    event.set()

        event.wait()
        with self._lock:
            return dict(self._results.get(mirror, {}))
