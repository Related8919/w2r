import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.manual_pdf_access_check import (
    international_pdf_url,
    main,
    probe_pdf,
)


class ManualPdfAccessCheckTests(unittest.TestCase):
    def test_pdf_url_changes_only_hostname_suffix(self):
        self.assertEqual(
            international_pdf_url(
                "https://manual.example.cn/owners/modely/zh_cn/index.html"
            ),
            "https://manual.example.com/owners/modely/zh_cn/Owners_Manual.pdf",
        )

    def test_probe_accepts_partial_pdf_response(self):
        response = Mock()
        response.status_code = 206
        response.url = "https://manual.example.com/Owners_Manual.pdf"
        response.headers = {
            "Content-Type": "application/pdf",
            "Content-Range": "bytes 0-511/1000",
        }
        response.iter_content.return_value = iter([b"%PDF-1.7 test"])
        session = Mock()
        session.trust_env = False
        session.get.return_value = response

        result = probe_pdf(session, response.url, 30)

        self.assertTrue(result["ok"])
        self.assertTrue(result["pdf_signature"])
        self.assertTrue(result["content_type_accepted"])
        self.assertTrue(result["environment_proxy_disabled"])
        response.close.assert_called_once()

    def test_probe_reports_access_denied_reference(self):
        response = Mock()
        response.status_code = 403
        response.url = "https://manual.example.com/Owners_Manual.pdf"
        response.headers = {
            "Content-Type": "text/html",
            "Server": "AkamaiGHost",
            "X-Reference-Error": "reference-id",
        }
        response.iter_content.return_value = iter([b"<html>Access Denied</html>"])
        session = Mock()
        session.trust_env = False
        session.get.return_value = response

        result = probe_pdf(session, response.url, 30)

        self.assertFalse(result["ok"])
        self.assertEqual(result["status_code"], 403)
        self.assertFalse(result["content_type_accepted"])
        self.assertEqual(result["reference_error"], "reference-id")
        self.assertIn("Access Denied", result["body_preview"])

    @patch("scripts.manual_pdf_access_check.probe_pdf")
    def test_main_writes_failure_result_and_returns_nonzero(self, probe):
        probe.return_value = {
            "ok": False,
            "status_code": 403,
            "reference_error": "reference-id",
        }
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            exit_code = main(
                [
                    "--mainland-page-url",
                    "https://manual.example.cn/modely/zh_cn/index.html",
                    "--result-file",
                    str(result_path),
                ]
            )

            self.assertEqual(exit_code, 1)
            self.assertEqual(
                json.loads(result_path.read_text(encoding="utf-8")),
                probe.return_value,
            )


if __name__ == "__main__":
    unittest.main()
