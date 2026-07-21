import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.manual_version_monitor import (
    Target,
    build_bark_url,
    extract_pdf_text,
    fetch_manual_content,
    make_text_diff,
    normalize_text,
    parse_manual_page,
    process_target,
    send_bark,
    update_rss,
)


PAGE_URL = "https://example.com/manual/modely/"
VERSION_CSS = 'article[role="article"] .body > p.p'
PDF_CSS = 'footer#footer a[href$="Owners_Manual.pdf"]'


def page(version="2026.8", extra=""):
    return f'''<article role="article"><div class="body">
      {extra}
      <p class="p"> 软件版本: <span><span>{version}</span></span> </p>
    </div></article>
    <footer id="footer"><a href="./Owners_Manual.pdf">Download PDF</a></footer>'''.encode()


class ManualVersionMonitorTests(unittest.TestCase):
    def test_bark_token_is_encoded_in_url_path(self):
        url = build_bark_url(
            "https://api.day.app", "secret/token", "title", "body",
            "https://example/manual.pdf", "group",
        )
        self.assertIn("/secret%2Ftoken/title/body?", url)
        self.assertIn("url=https%3A%2F%2Fexample%2Fmanual.pdf", url)

    def test_bark_request_error_does_not_expose_token(self):
        session = Mock()
        session.get.side_effect = __import__("requests").RequestException(
            "failed https://api.day.app/secret-token/title/body"
        )
        with self.assertRaisesRegex(RuntimeError, "Bark request failed") as context:
            send_bark(
                session, "https://api.day.app", "secret-token", "title", "body",
                "https://example/manual.pdf", "group", 30,
            )
        self.assertNotIn("secret-token", str(context.exception))

    def test_normalize_text_collapses_space_and_colon(self):
        self.assertEqual(normalize_text(" 软件版本:  2026.8\n"), "软件版本：2026.8")

    def test_parse_manual_page_finds_text_regardless_of_paragraph_order(self):
        html = page(extra='<p class="p">其他文字</p>')
        version, pdf_url = parse_manual_page(
            html, PAGE_URL, VERSION_CSS, "软件版本：", PDF_CSS
        )
        self.assertEqual(version, "软件版本：2026.8")
        self.assertEqual(pdf_url, f"{PAGE_URL}Owners_Manual.pdf")

    def test_parse_manual_page_rejects_zero_or_multiple_versions(self):
        missing = b'''<article role="article"><div class="body"><p class="p">none</p>
        </div></article><footer id="footer"><a href="Owners_Manual.pdf">PDF</a></footer>'''
        with self.assertRaisesRegex(ValueError, "found 0"):
            parse_manual_page(missing, PAGE_URL, VERSION_CSS, "软件版本：", PDF_CSS)

        duplicate = page(extra='<p class="p">软件版本：2025.44</p>')
        with self.assertRaisesRegex(ValueError, "found 2"):
            parse_manual_page(duplicate, PAGE_URL, VERSION_CSS, "软件版本：", PDF_CSS)

    def test_parse_manual_page_requires_one_pdf_link(self):
        html = page() + b'<footer id="footer"><a href="second/Owners_Manual.pdf">PDF</a></footer>'
        with self.assertRaisesRegex(ValueError, "found 2"):
            parse_manual_page(html, PAGE_URL, VERSION_CSS, "软件版本：", PDF_CSS)

    def test_make_text_diff_counts_changes(self):
        diff, additions, deletions = make_text_diff("one\ntwo", "one\nthree")
        self.assertIn("+three", diff)
        self.assertEqual((additions, deletions), (1, 1))

    def test_empty_pdf_text_has_explicit_message(self):
        diff, additions, deletions = make_text_diff("", "")
        self.assertIn("无可提取的文本层", diff)
        self.assertEqual((additions, deletions), (0, 0))

    def test_extract_pdf_text_handles_blank_page(self):
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with path.open("wb") as output:
                writer.write(output)
            self.assertEqual(extract_pdf_text(path), "===== Page 1 =====")

    def test_browser_fetcher_is_used_without_http_request(self):
        browser = Mock()
        browser.page_html.return_value = b"browser html"
        self.assertEqual(
            fetch_manual_content(PAGE_URL, browser, "软件版本："), b"browser html"
        )
        browser.page_html.assert_called_once_with(PAGE_URL, "软件版本：")

    def test_first_run_creates_baseline_and_empty_rss(self):
        session = Mock()
        browser = Mock()
        browser.page_html.return_value = page()
        browser.download.return_value = b"%PDF-fake bytes"
        state = {"schema_version": 1, "targets": {}}
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            target = Target(
                "modely", "Model Y", PAGE_URL,
                directory_path / "feed.xml", "current.pdf",
            )
            result = process_target(
                session, target, state, directory_path, VERSION_CSS, "软件版本：",
                PDF_CSS, "", "gemini-2.5-flash", 30, 50, browser,
                "https://api.day.app", "token", "title", "group",
            )
            self.assertEqual(result, "baseline")
            self.assertTrue((directory_path / "current.pdf").exists())
            self.assertTrue(target.rss_path.exists())
            self.assertEqual(
                ET.parse(target.rss_path).getroot().findall("./channel/item"), []
            )

    def test_unchanged_version_does_not_download_pdf(self):
        session = Mock()
        browser = Mock()
        browser.page_html.return_value = page()
        state = {"schema_version": 1, "targets": {"modely": {"version": "软件版本：2026.8"}}}
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            (directory_path / "current.pdf").write_bytes(b"%PDF-current")
            target = Target(
                "modely", "Model Y", PAGE_URL, Path("unused.xml"), "current.pdf"
            )
            result = process_target(
                session, target, state, directory_path, VERSION_CSS, "软件版本：",
                PDF_CSS, "", "gemini-2.5-flash", 30, 50, browser,
                "https://api.day.app", "token", "title", "group",
            )
            self.assertEqual(result, "unchanged")
            session.get.assert_not_called()
            browser.download.assert_not_called()

    def test_changed_version_updates_pdf_state_and_rss(self):
        session = Mock()
        bark_response = Mock(status_code=200)
        bark_response.json.return_value = {"code": 200}
        session.get.return_value = bark_response
        browser = Mock()
        browser.page_html.return_value = page("2026.12")
        browser.download.return_value = b"%PDF-new pdf"
        state = {
            "schema_version": 1,
            "targets": {
                "modely": {
                    "version": "软件版本：2026.8",
                    "pdf_url": f"{PAGE_URL}Owners_Manual.pdf",
                    "pdf_sha256": "old-hash",
                    "pdf_asset": "current.pdf",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            asset = directory_path / "current.pdf"
            asset.write_bytes(b"old pdf")
            target = Target(
                "modely", "Model Y", PAGE_URL,
                directory_path / "feed.xml", "current.pdf",
            )
            with patch(
                "scripts.manual_version_monitor.extract_pdf_text",
                side_effect=["old line", "new line"],
            ), patch(
                "scripts.manual_version_monitor.summarize_with_gemini",
                return_value="Gemini summary",
            ):
                result = process_target(
                    session, target, state, directory_path, VERSION_CSS,
                    "软件版本：", PDF_CSS, "key", "gemini-2.5-flash",
                    30, 50, browser, "https://api.day.app", "token", "title", "group",
                )

            self.assertEqual(result, "updated")
            self.assertEqual(asset.read_bytes(), b"%PDF-new pdf")
            self.assertEqual(
                state["targets"]["modely"]["version"], "软件版本：2026.12"
            )
            item = ET.parse(target.rss_path).getroot().find("./channel/item")
            self.assertEqual(
                item.findtext("title"), "Model Y 车主手册更新（软件版本：2026.12）"
            )
            self.assertIn("Gemini summary", item.findtext("description"))
            session.get.assert_called_once()

    def test_rss_deduplicates_and_limits_items(self):
        target = Target("modely", "Model Y", PAGE_URL, Path("unused.xml"), "current.pdf")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            for index in range(4):
                update_rss(
                    path, target, f"old-{index}", f"new-{index}", "https://example/pdf",
                    f"hash-{index}", "summary", 1, 1,
                    datetime(2026, 1, index + 1, tzinfo=timezone.utc), 3,
                )
            update_rss(
                path, target, "old-3", "new-3", "https://example/pdf", "hash-3",
                "summary", 1, 1, datetime.now(timezone.utc), 3,
            )
            items = ET.parse(path).getroot().findall("./channel/item")
            self.assertEqual(len(items), 3)
            self.assertEqual(items[0].findtext("guid"), "manual-modely-new-3-hash-3")


if __name__ == "__main__":
    unittest.main()
