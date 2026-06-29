import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scihub.cli import build_parser, parse_journal_file
from scihub.metadata import DiscoveredArticle, OpenAlexClient


class CliAndMetadataTests(unittest.TestCase):
    def test_parser_uses_subcommands_without_legacy_default_download(self):
        parser = build_parser()

        self.assertEqual(parser.parse_args(["init-db"]).command, "init-db")
        self.assertEqual(parser.parse_args(["import-journals", "-i", "name.txt"]).command, "import-journals")
        self.assertEqual(parser.parse_args(["discover", "--limit-per-journal", "25"]).limit_per_journal, 25)
        self.assertEqual(parser.parse_args(["download", "--limit", "3", "--no-interactive"]).command, "download")

        with self.assertRaises(SystemExit):
            parser.parse_args([])

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


if __name__ == "__main__":
    unittest.main()
