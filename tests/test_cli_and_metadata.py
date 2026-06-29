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

    def test_settings_include_year_range_from_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("PAPER_FROM_YEAR=2000\nPAPER_TO_YEAR=2024\n", encoding="utf-8")

            args = build_parser().parse_args(["--env-file", str(env_path), "discover"])
            settings = load_settings(args)

            self.assertEqual(settings.from_year, 2000)
            self.assertEqual(settings.to_year, 2024)

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
            articles = list(client.iter_articles("Nature", limit=10, from_year=2020, to_year=2024))

        self.assertEqual(
            articles,
            [DiscoveredArticle(doi="10.1038/example", title="Example", year=2024, source="openalex")],
        )

    def test_openalex_client_applies_year_filters_to_query(self):
        seen_urls = []
        payload = {"results": [], "meta": {"next_cursor": None}}

        class FakeResponse:
            status_code = 200
            text = json.dumps(payload)

            def json(self):
                return payload

        def fake_urlopen_json(url):
            seen_urls.append(url)
            return FakeResponse()

        with patch("scihub.metadata.urlopen_json", side_effect=fake_urlopen_json):
            list(OpenAlexClient().iter_articles("Nature", limit=10, from_year=2020, to_year=2024))

        self.assertIn("from_publication_date%3A2020-01-01", seen_urls[0])
        self.assertIn("to_publication_date%3A2024-12-31", seen_urls[0])

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
            articles = list(client.iter_articles("Journal of Examples", limit=10, from_year=2020, to_year=2024))

        self.assertEqual(
            articles,
            [DiscoveredArticle(doi="10.1021/example", title="Crossref example", year=2023, source="crossref")],
        )

    def test_crossref_client_uses_cursor_pagination_and_year_filters(self):
        seen_urls = []
        payloads = [
            {
                "message": {
                    "next-cursor": "next-token",
                    "items": [{"DOI": "10.1021/one", "title": ["One"], "issued": {"date-parts": [[2022]]}}],
                }
            },
            {
                "message": {
                    "next-cursor": "next-token",
                    "items": [{"DOI": "10.1021/two", "title": ["Two"], "issued": {"date-parts": [[2023]]}}],
                }
            },
        ]

        class FakeResponse:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload
                self.text = json.dumps(payload)

            def json(self):
                return self._payload

        def fake_urlopen_json(url):
            seen_urls.append(url)
            return FakeResponse(payloads.pop(0))

        with patch("scihub.metadata.urlopen_json", side_effect=fake_urlopen_json):
            articles = list(CrossrefClient(rows=1).iter_articles("Journal", limit=2, from_year=2020, to_year=2024))

        self.assertEqual([article.doi for article in articles], ["10.1021/one", "10.1021/two"])
        self.assertIn("cursor=%2A", seen_urls[0])
        self.assertIn("cursor=next-token", seen_urls[1])
        self.assertIn("from-pub-date%3A2020-01-01", seen_urls[0])
        self.assertIn("until-pub-date%3A2024-12-31", seen_urls[0])


if __name__ == "__main__":
    unittest.main()
