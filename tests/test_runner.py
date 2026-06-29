import unittest

from scihub.runner import classify_error


class RunnerTests(unittest.TestCase):
    def test_classify_download_errors(self):
        self.assertEqual(classify_error("HTTP 429"), "rate_limited")
        self.assertEqual(classify_error("HTTP 503"), "http_error")
        self.assertEqual(classify_error("returned content is not a PDF"), "invalid_pdf")
        self.assertEqual(classify_error("captcha required"), "captcha_required")
        self.assertEqual(classify_error("request failed: timeout"), "network_error")
        self.assertEqual(classify_error("all mirrors failed"), "mirror_failed")


if __name__ == "__main__":
    unittest.main()
