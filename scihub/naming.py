"""DOI normalisation: building request URLs and filesystem-safe filenames."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

# Characters that are illegal or troublesome in filenames on macOS/Windows/Linux.
_UNSAFE_FOR_FILENAME = re.compile(r'[\\/:*?"<>|;#%\s]+')
_COLLAPSE_UNDERSCORE = re.compile(r"_+")


def normalize_doi(raw: str) -> str:
    """Strip surrounding whitespace and a leading ``doi:`` prefix if present."""
    doi = raw.strip()
    low = doi.lower()
    for prefix in ("doi:", "https://doi.org/", "http://doi.org/"):
        if low.startswith(prefix):
            doi = doi[len(prefix):].strip()
            break
    return doi


def doi_to_url(mirror: str, doi: str) -> str:
    """Build the Sci-Hub detail-page URL for a DOI on a given mirror.

    Only characters that would break URL parsing are percent-encoded (``<``,
    ``>``, ``#`` and whitespace); structural DOI characters such as ``/ : ; ( )``
    are kept verbatim, matching how Sci-Hub links are formed.
    """
    encoded = quote(normalize_doi(doi), safe="/:;().,=")
    return f"https://{mirror}/{encoded}"


def doi_to_filename(doi: str) -> str:
    """Turn a DOI into a safe, collision-resistant ``.pdf`` filename."""
    name = _UNSAFE_FOR_FILENAME.sub("_", normalize_doi(doi))
    name = _COLLAPSE_UNDERSCORE.sub("_", name).strip("_")
    return f"{name}.pdf"


def doi_to_readable_pdf_path(root: Path, doi: str) -> Path:
    """Build a readable, DOI-grouped PDF path under ``root``."""
    normalized = normalize_doi(doi)
    prefix = normalized.split("/", 1)[0] if "/" in normalized else "unknown"
    safe_prefix = _UNSAFE_FOR_FILENAME.sub("_", prefix).strip("_") or "unknown"
    return Path(root) / "by_doi" / safe_prefix / doi_to_filename(normalized)
