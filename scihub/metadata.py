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


class CrossrefClient:
    def __init__(self, mailto: str | None = None, rows: int = 100) -> None:
        self.mailto = mailto
        self.rows = rows

    def iter_articles(self, journal_name: str, limit: int | None = None) -> Iterator[DiscoveredArticle]:
        yielded = 0
        offset = 0
        seen: set[str] = set()
        while True:
            rows = min(self.rows, limit or self.rows)
            params = {
                "query.container-title": journal_name,
                "filter": "type:journal-article,has-full-text:true",
                "rows": str(rows),
                "offset": str(offset),
            }
            if self.mailto:
                params["mailto"] = self.mailto
            response = urlopen_json("https://api.crossref.org/works?" + urlencode(params))
            if response.status_code != 200:
                raise RuntimeError(f"Crossref HTTP {response.status_code}: {response.text[:200]}")
            items = (response.json().get("message") or {}).get("items") or []
            if not items:
                return
            for item in items:
                doi = self._clean_doi(item.get("DOI"))
                if not doi or doi in seen:
                    continue
                seen.add(doi)
                yield DiscoveredArticle(
                    doi=doi,
                    title=self._title(item),
                    year=self._year(item),
                    source="crossref",
                )
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            if len(items) < rows:
                return
            offset += len(items)

    @staticmethod
    def _clean_doi(raw: str | None) -> str:
        if not raw:
            return ""
        return normalize_doi(raw).lower()

    @staticmethod
    def _title(item: dict) -> str:
        title = item.get("title")
        if isinstance(title, list) and title:
            return str(title[0])
        if isinstance(title, str):
            return title
        return ""

    @staticmethod
    def _year(item: dict) -> int | None:
        for key in ("published-print", "published-online", "published", "issued"):
            date_parts = (item.get(key) or {}).get("date-parts")
            if date_parts and date_parts[0]:
                try:
                    return int(date_parts[0][0])
                except (TypeError, ValueError):
                    return None
        return None
