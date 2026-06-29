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

Discover DOI records from OpenAlex:

```bash
python download_papers.py discover --limit-per-journal 100 --mailto your@email.com
```

Download pending DOI jobs:

```bash
python download_papers.py download --limit 50
```

Show queue status:

```bash
python download_papers.py status
```

Move failed jobs back to pending:

```bash
python download_papers.py retry-failed
```

Run without opening a browser for manual captcha solving:

```bash
python download_papers.py download --limit 50 --no-interactive
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
- PDF writes remain atomic: files are written as `.part` first and renamed only
  after the `%PDF` signature is verified.
- For large runs, use small batches such as `download --limit 50` and inspect
  `status` between batches.
