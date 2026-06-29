"""PDF download with streaming, signature verification, and atomic writes.

Never overwrites in place: writes to ``<name>.part`` and renames on success, so
a crash or a challenge-page-disguised-as-PDF never leaves a corrupt final file.
"""
from __future__ import annotations

from pathlib import Path

from . import config
from .fetch import MirrorSession

_CHUNK = 64 * 1024
_HEADER_PROBE = 2048  # bytes inspected for the %PDF signature


def download_pdf(session: MirrorSession, pdf_url: str, referer: str, dest: Path) -> tuple[bool, str]:
    """Stream ``pdf_url`` to ``dest``. Returns ``(ok, detail)``."""
    try:
        resp = session.get(pdf_url, referer=referer, stream=True)
    except Exception as exc:
        return False, f"请求异常: {exc}"

    try:
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}"

        tmp = dest.with_name(dest.name + ".part")
        header = b""
        verified = False
        written = 0

        with open(tmp, "wb") as handle:
            for chunk in resp.iter_content(chunk_size=_CHUNK):
                if not chunk:
                    continue
                if not verified:
                    header += chunk
                    if config.PDF_MAGIC in header[:_HEADER_PROBE]:
                        verified = True
                    elif len(header) >= _HEADER_PROBE:
                        # Enough bytes seen and still no signature: it's HTML
                        # (a challenge page) or junk, not a PDF.
                        break
                handle.write(chunk)
                written += len(chunk)

        if not verified or written == 0:
            tmp.unlink(missing_ok=True)
            return False, "返回内容不是 PDF（可能是验证页或空响应）"

        tmp.replace(dest)
        return True, f"{written / 1024:.0f} KB"
    finally:
        try:
            resp.close()
        except Exception:
            pass


def save_bytes_as_pdf(content: bytes, dest: Path) -> tuple[bool, str]:
    """Persist an already-downloaded body (when the mirror serves the PDF directly)."""
    if not content.startswith(config.PDF_MAGIC):
        return False, "响应非 PDF"
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(content)
    tmp.replace(dest)
    return True, f"{len(content) / 1024:.0f} KB"
