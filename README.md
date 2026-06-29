# Local Journal Paper Downloader

This project is a local, long-running CLI pipeline for collecting paper PDFs.
It imports journal names from `name.txt`, discovers DOI records through
OpenAlex, stores work in SQLite, and downloads queued PDFs through Sci-Hub.

## Install

```bash
uv pip install -r requirements.txt
playwright install chromium
```

If you use the checked-in virtual environment, run commands with:

```bash
.\.venv\Scripts\python.exe download_papers.py --help
```

## Configure

Copy `.env.example` to `.env`, then edit the values for your local run:

```env
PAPER_DB=data/papers.db
PAPER_JOURNAL_FILE=name.txt
PAPER_OUTPUT_DIR=papers
PAPER_COOKIE_DIR=.cookies

PAPER_MAILTO=your@email.com
PAPER_LIMIT_PER_JOURNAL=100
PAPER_FROM_YEAR=
PAPER_TO_YEAR=

PAPER_DOWNLOAD_WORKERS=4
PAPER_PER_MIRROR_WORKERS=1
PAPER_DOWNLOAD_POLL_SECONDS=30
PAPER_STOP_WHEN_IDLE=true
PAPER_MAX_ATTEMPTS=5
PAPER_RETRY_BASE_MINUTES=30
PAPER_RETRY_MAX_HOURS=24
PAPER_INTERACTIVE_CAPTCHA=true
PAPER_LOG_DIR=logs

PAPER_MIN_DELAY=2.0
PAPER_MAX_DELAY=5.0
PAPER_NO_INTERACTIVE=false
PAPER_MIRRORS=sci-hub.ru,sci-hub.st,sci-hub.su,sci-hub.box
```

CLI flags override `.env`, and `.env` overrides built-in defaults.

## Data Model

SQLite stores journals, discovered articles, download jobs, PDF file metadata,
and event history. PDF bytes are stored on disk, not inside SQLite.

Default locations:

```text
data/papers.db
papers/by_doi/<doi-prefix>/<safe-doi>.pdf
.cookies/<mirror>.json
```

Example PDF path:

```text
papers/by_doi/10.1002/10.1002_(sici)(1997)5_1_1_aid-nt1_3.0.co_2-8.pdf
```

## CLI Workflow

Initialize the database:

```bash
python download_papers.py init-db
```

Import journals from `name.txt`:

```bash
python download_papers.py import-journals -i name.txt
```

Discover DOI records from OpenAlex and Crossref:

```bash
python download_papers.py discover
```

Optionally limit discovery by publication year in `.env`:

```env
PAPER_FROM_YEAR=2000
PAPER_TO_YEAR=2026
```

Run the concurrent downloader. By default it processes ready jobs until the
queue is idle:

```bash
python download_papers.py download
```

For a test run, cap the total processed jobs:

```bash
python download_papers.py download --max-jobs 100
```

Show queue status:

```bash
python download_papers.py status
```

Move failed jobs back to pending:

```bash
python download_papers.py retry-failed
```

Move only one failure type back to pending:

```bash
python download_papers.py retry-failed --error-type network_error
```

Inspect recent events and export results:

```bash
python download_papers.py recent-events --limit 50
python download_papers.py export-failed -o failed.csv
python download_papers.py export-downloaded -o downloaded.csv
```

Disable browser captcha solving:

```bash
python download_papers.py download --no-interactive
```

## `name.txt` Format

Each non-empty, non-comment line can be either:

```text
Journal Name
Journal Name<TAB>https://journal-homepage.example
```

The current `name.txt` uses the second form.

## Notes

- The CLI no longer supports the old implicit `links.txt` mode. All work should
  go through the database-backed subcommands.
- Downloads are resumable: downloaded jobs are marked in SQLite and existing
  PDF files are skipped.
- DOI discovery is also resilient: if one metadata source fails for a journal,
  the failure is recorded in SQLite events and the next source/journal continues.
- OpenAlex and Crossref discoveries are deduplicated by DOI, while the original
  metadata sources are preserved in `article_sources`.
- The downloader is concurrent. Global concurrency defaults to 4 workers, and
  per-mirror concurrency defaults to 1 worker.
- Captcha/browser solving is serialized: only one Playwright window opens at a
  time, and workers waiting on the same mirror reuse the saved cookie result.
- PDF writes remain atomic: files are written as unique `.part.<job_id>` files
  first and renamed only after the `%PDF` signature is verified.
