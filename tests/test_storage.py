import sqlite3
import tempfile
import unittest
from pathlib import Path

from scihub.naming import doi_to_readable_pdf_path
from scihub.storage import ArticleRecord, Database


class StorageTests(unittest.TestCase):
    def test_schema_imports_journals_and_deduplicates_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "papers.db")
            db.init()

            imported = db.import_journals([
                ("Nature", "https://www.nature.com/nature/"),
                ("Nature", "https://duplicate.example"),
                ("Science", "https://www.science.org/journal/science"),
            ])

            self.assertEqual(imported, 2)
            journals = db.list_journals()
            self.assertEqual([journal["name"] for journal in journals], ["Nature", "Science"])
            self.assertEqual(journals[0]["source_url"], "https://www.nature.com/nature/")

    def test_upsert_article_creates_single_pending_download_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "papers.db")
            db.init()
            journal_id = db.import_journals([("Nature", "")])

            article = ArticleRecord(
                doi="10.1038/example",
                journal_id=journal_id,
                title="Example article",
                year=2024,
                source="openalex",
            )
            first = db.upsert_article(article)
            second = db.upsert_article(article)

            self.assertEqual(first, second)
            jobs = db.get_jobs(limit=10)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["doi"], "10.1038/example")
            self.assertEqual(jobs[0]["status"], "pending")

    def test_mark_downloaded_records_pdf_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "papers.db")
            db.init()
            db.import_journals([("Nature", "")])
            db.upsert_article(ArticleRecord(doi="10.1038/example", journal_id=1, source="test"))

            db.mark_downloaded(
                doi="10.1038/example",
                path=Path("papers/by_doi/10.1038/10.1038_example.pdf"),
                size_bytes=1234,
                sha256="abc123",
                mirror="sci-hub.st",
            )

            conn = sqlite3.connect(Path(tmp) / "papers.db")
            try:
                row = conn.execute(
                    "select status, file_path, size_bytes, sha256, mirror from pdf_files where doi = ?",
                    ("10.1038/example",),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row, ("downloaded", "papers/by_doi/10.1038/10.1038_example.pdf", 1234, "abc123", "sci-hub.st"))
            self.assertEqual(db.get_jobs(limit=10)[0]["status"], "downloaded")

    def test_doi_readable_path_groups_by_prefix(self):
        path = doi_to_readable_pdf_path(Path("papers"), "10.1002/(sici)(1997)5:1<1::aid-nt1>3.0.co;2-8")

        self.assertEqual(
            path,
            Path("papers") / "by_doi" / "10.1002" / "10.1002_(sici)(1997)5_1_1_aid-nt1_3.0.co_2-8.pdf",
        )


if __name__ == "__main__":
    unittest.main()
