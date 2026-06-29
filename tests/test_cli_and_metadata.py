import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scihub.cli import build_parser, load_settings, parse_journal_file
from scihub.metadata import CrossrefClient, DiscoveredArticle, OpenAlexClient


class CliAndMetadataTests(unittest.TestCase):
    def test_parser_uses_subcommands_without_legacy_default_download(self):
        parser = build_parser()

        self.assertEqual(parser.parse_args(["init-db"]).command, "init-db")
        self.assertEqual(parser.parse_args(["import-journals", "-i", "name.txt"]).command, "import-journals")
        self.assertEqual(parser.parse_args(["discover", "--limit-per-journal", "25"]).limit_per_journal, 25)
        self.assertEqual(parser.parse_args(["download", "--limit", "3", "--no-interactive"]).command, "download")

        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_settings_load_from_env_and_cli_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "PAPER_DB=data/custom.db\n"
                "PAPER_JOURNAL_FILE=journals.txt\n"
                "PAPER_OUTPUT_DIR=library\n"
                "PAPER_COOKIE_DIR=.paper-cookies\n"
                "PAPER_MAILTO=reader@example.com\n"
                "PAPER_LIMIT_PER_JOURNAL=25\n"
                "PAPER_DOWNLOAD_LIMIT=75\n"
                "PAPER_NO_INTERACTIVE=true\n"
                "PAPER_MIRRORS=sci-hub.st,sci-hub.ru\n",
                encoding="utf-8",
            )

            parser = build_parser()
            args = parser.parse_args(["--env-file", str(env_path), "download", "--limit", "3"])
            settings = load_settings(args)

            self.assertEqual(settings.db, "data/custom.db")
            self.assertEqual(settings.journal_file, "journals.txt")
            self.assertEqual(settings.output_dir, "library")
            self.assertEqual(settings.cookie_dir, ".paper-cookies")
            self.assertEqual(settings.mailto, "reader@example.com")
            self.assertEqual(settings.limit_per_journal, 25)
            self.assertEqual(settings.download_limit, 3)
            self.assertTrue(settings.no_interactive)
            self.assertEqual(settings.mirrors, ["sci-hub.st", "sci-hub.ru"])

    def test_parse_journal_file_accepts_tab_or_plain_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "name.txt"
            path.write_text(
                "Nature\thttps://www.nature.com/nature/\n"
                "# comment\n"
                "\n"
                "Science\n",
                encoding="utf-8",
            )

            self.assertEqual(
                parse_journal_file(path),
                [("Nature", "https://www.nature.com/nature/"), ("Science", "")],
            )

    def test_openalex_client_extracts_articles_with_doi(self):
        payload = {
            "results": [
                {
                    "doi": "https://doi.org/10.1038/example",
                    "title": "Example",
                    "publication_year": 2024,
                },
                {"doi": None, "title": "No DOI"},
            ],
            "meta": {"next_cursor": None},
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(payload)

            def json(self):
                return payload

        with patch("scihub.metadata.urlopen_json", return_value=FakeResponse()):
            client = OpenAlexClient(mailto="test@example.com")
            articles = list(client.iter_articles("Nature", limit=10))

        self.assertEqual(
            articles,
            [DiscoveredArticle(doi="10.1038/example", title="Example", year=2024, source="openalex")],
        )

    def test_crossref_client_extracts_articles_with_doi(self):
        payload = {
            "message": {
                "items": [
                    {
                        "DOI": "10.1021/example",
                        "title": ["Crossref example"],
                        "published-print": {"date-parts": [[2023, 1, 1]]},
                    },
                    {"title": ["No DOI"]},
                ]
            }
        }

        class FakeResponse:
            status_code = 200
            text = json.dumps(payload)

            def json(self):
                return payload

        with patch("scihub.metadata.urlopen_json", return_value=FakeResponse()):
            client = CrossrefClient(mailto="test@example.com")
            articles = list(client.iter_articles("Journal of Examples", limit=10))

        self.assertEqual(
            articles,
            [DiscoveredArticle(doi="10.1021/example", title="Crossref example", year=2023, source="crossref")],
        )


if __name__ == "__main__":
    unittest.main()
