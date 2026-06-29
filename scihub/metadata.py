"""Journal metadata discovery via public APIs."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterator
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .naming import normalize_doi


@dataclass(frozen=True)
class DiscoveredArticle:
    doi: str
    title: str = ""
    year: int | None = None
    source: str = "openalex"


class JsonResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text

    def json(self) -> dict:
        return json.loads(self.text)


def urlopen_json(url: str, timeout: int = 45) -> JsonResponse:
    req = Request(url, headers={"User-Agent": "paper-download/1.0"})
    with urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return JsonResponse(getattr(resp, "status", 200), body)


class OpenAlexClient:
    def __init__(self, mailto: str | None = None, per_page: int = 200) -> None:
        self.mailto = mailto
        self.per_page = per_page

    def iter_articles(self, journal_name: str, limit: int | None = None) -> Iterator[DiscoveredArticle]:
        yielded = 0
        cursor = "*"
        while True:
            params = {
                "filter": f"primary_location.source.display_name.search:{journal_name}",
                "per-page": str(min(self.per_page, limit or self.per_page)),
                "cursor": cursor,
            }
            if self.mailto:
                params["mailto"] = self.mailto
            response = urlopen_json("https://api.openalex.org/works?" + urlencode(params))
            if response.status_code != 200:
                raise RuntimeError(f"OpenAlex HTTP {response.status_code}: {response.text[:200]}")
            payload = response.json()
            for item in payload.get("results", []):
                doi = self._clean_doi(item.get("doi"))
                if not doi:
                    continue
                yield DiscoveredArticle(
                    doi=doi,
                    title=str(item.get("title") or ""),
                    year=item.get("publication_year"),
                    source="openalex",
                )
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            cursor = (payload.get("meta") or {}).get("next_cursor")
            if not cursor:
                return

    @staticmethod
    def _clean_doi(raw: str | None) -> str:
        if not raw:
            return ""
        return normalize_doi(raw).lower()
