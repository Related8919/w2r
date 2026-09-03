import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.manual_version_monitor import (
    build_region_targets,
    build_bark_url,
    complete_rss_entry,
    configured_targets,
    derive_international_url,
    extract_pdf_text,
    fallback_summary,
    fetch_manual_content,
    load_state,
    make_text_diff,
    normalize_text,
    parse_manual_page,
    pending_rss_entry,
    process_target,
    send_bark,
)


PAGE_URL = "https://manual.example.cn/manual/modely/"
VERSION_CSS = 'article[role="article"] .body > p.p'
PDF_CSS = 'footer#footer a[href$="Owners_Manual.pdf"]'


def page(version="2026.8", extra=""):
    return f'''<article role="article"><div class="body">
      {extra}
      <p class="p"> 软件版本: <span><span>{version}</span></span> </p>
    </div></article>
    <footer id="footer"><a href="./Owners_Manual.pdf">Download PDF</a></footer>'''.encode()


def modely_targets(path):
    return build_region_targets(
        "modely",
        "Model Y",
        PAGE_URL,
        path,
        "modely-current.pdf",
        "modely-international-current.pdf",
    )


def pending_feed(path, target, version="软件版本：2026.8", guid=None):
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = target.item_title(version)
    ET.SubElement(item, "link").text = f"{target.page_url}Owners_Manual.pdf"
    ET.SubElement(item, "guid").text = guid or target.guid_for(version)
    ET.SubElement(item, "description").text = ""
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


class ManualVersionMonitorTests(unittest.TestCase):
    def test_derive_international_url_changes_only_hostname_suffix(self):
        mainland = (
            "https://www.example.cn:8443/owners/zh_cn/index.html?region=cn#cn"
        )
        self.assertEqual(
            derive_international_url(mainland),
            "https://www.example.com:8443/owners/zh_cn/index.html?region=cn#cn",
        )

    def test_derive_international_url_rejects_invalid_mainland_url(self):
        for value in (
            "ftp://www.example.cn/manual",
            "https:///manual",
            "https://www.example.com/manual",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    derive_international_url(value)

    def test_configured_targets_builds_two_regions_for_each_model(self):
        environment = {
            "MODEL3_MANUAL_URL": "https://manual.example.cn/model3/zh_cn/",
            "MODELY_MANUAL_URL": "https://manual.example.cn/modely/zh_cn/",
            "MODEL3_MANUAL_RSS_PATH": "rss/model3.xml",
            "MODELY_MANUAL_RSS_PATH": "rss/modely.xml",
        }
        with patch.dict(os.environ, environment, clear=False):
            targets = configured_targets()

        self.assertEqual(
            [target.key for target in targets],
            ["model3_cn", "model3_com", "modely_cn", "modely_com"],
        )
        self.assertEqual(
            [target.page_url for target in targets],
            [
                "https://manual.example.cn/model3/zh_cn/",
                "https://manual.example.com/model3/zh_cn/",
                "https://manual.example.cn/modely/zh_cn/",
                "https://manual.example.com/modely/zh_cn/",
            ],
        )
        self.assertEqual(
            [target.pdf_asset for target in targets],
            [
                "model3-manual-current.pdf",
                "model3-international-manual-current.pdf",
                "modely-manual-current.pdf",
                "modely-international-manual-current.pdf",
            ],
        )

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

    def test_pending_entry_and_completion_update_same_rss_item(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            target, _ = modely_targets(path)
            pending_feed(path, target)
            self.assertEqual(
                pending_rss_entry(path, target),
                (
                    "软件版本：2026.8",
                    f"{PAGE_URL}Owners_Manual.pdf",
                    "manual-modely-cn-软件版本：2026.8",
                ),
            )
            complete_rss_entry(
                path, "manual-modely-cn-软件版本：2026.8", "summary"
            )
            self.assertIsNone(pending_rss_entry(path, target))
            self.assertEqual(
                ET.parse(path).getroot().findtext("./channel/item/description"),
                "summary",
            )

    def test_pending_entries_are_selected_by_region(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            mainland, international = modely_targets(path)
            root = ET.Element("rss", {"version": "2.0"})
            channel = ET.SubElement(root, "channel")
            for target in (international, mainland):
                item = ET.SubElement(channel, "item")
                ET.SubElement(item, "title").text = target.item_title(
                    "软件版本：2026.8"
                )
                ET.SubElement(item, "link").text = (
                    f"{target.page_url}Owners_Manual.pdf"
                )
                ET.SubElement(item, "guid").text = target.guid_for(
                    "软件版本：2026.8"
                )
                ET.SubElement(item, "description").text = ""
            ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

            self.assertEqual(
                pending_rss_entry(path, mainland)[2],
                "manual-modely-cn-软件版本：2026.8",
            )
            self.assertEqual(
                pending_rss_entry(path, international)[2],
                "manual-modely-com-软件版本：2026.8",
            )

    def test_load_state_migrates_legacy_mainland_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            legacy_model3 = {
                "version": "软件版本：2026.8",
                "pdf_url": "https://manual.example.cn/model3/current.pdf",
                "pdf_sha256": "model3-hash",
                "pdf_asset": "model3-manual-current.pdf",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "rss_description": "existing model3 summary",
            }
            legacy_modely = {
                "version": "软件版本：2026.8",
                "pdf_url": "https://manual.example.cn/modely/current.pdf",
                "pdf_sha256": "modely-hash",
                "pdf_asset": "modely-manual-current.pdf",
            }
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "targets": {
                            "model3": legacy_model3,
                            "modely": legacy_modely,
                        },
                    }
                ),
                encoding="utf-8",
            )

            state = load_state(path)

            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(state["targets"]["model3_cn"], legacy_model3)
            self.assertEqual(state["targets"]["modely_cn"], legacy_modely)
            self.assertNotIn("model3", state["targets"])
            self.assertNotIn("modely", state["targets"])

    def test_first_run_completes_rss_and_creates_pdf_baseline(self):
        session = Mock()
        bark_response = Mock(status_code=200)
        bark_response.json.return_value = {"code": 200}
        session.get.return_value = bark_response
        browser = Mock()
        browser.page_html.return_value = page()
        browser.download.return_value = b"%PDF-fake bytes"
        state = {"schema_version": 2, "targets": {}}
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            target, _ = modely_targets(directory_path / "feed.xml")
            pending_feed(target.rss_path, target)
            result = process_target(
                session, target, state, directory_path, "软件版本：",
                "", "gemini-2.5-flash", 30, browser,
                "https://api.day.app", "token", "title", "group",
            )
            self.assertEqual(result, "baseline")
            self.assertTrue((directory_path / "modely-current.pdf").exists())
            self.assertIn(
                "首次记录",
                ET.parse(target.rss_path).getroot().findtext(
                    "./channel/item/description"
                ),
            )
            self.assertIn(
                "Model Y 大陆版",
                state["targets"]["modely_cn"]["rss_description"],
            )
            self.assertNotIn("bark_pending", state["targets"]["modely_cn"])

    def test_both_regions_create_independent_baselines_from_one_feed(self):
        session = Mock()
        bark_response = Mock(status_code=200)
        bark_response.json.return_value = {"code": 200}
        session.get.return_value = bark_response
        browser = Mock()
        browser.page_html.return_value = page()
        browser.download.side_effect = [b"%PDF-mainland", b"%PDF-international"]
        state = {"schema_version": 2, "targets": {}}

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            feed_path = directory_path / "feed.xml"
            mainland, international = modely_targets(feed_path)
            root = ET.Element("rss", {"version": "2.0"})
            channel = ET.SubElement(root, "channel")
            for target in (mainland, international):
                item = ET.SubElement(channel, "item")
                version = "软件版本：2026.8"
                ET.SubElement(item, "title").text = target.item_title(version)
                ET.SubElement(item, "link").text = (
                    f"{target.page_url}Owners_Manual.pdf"
                )
                ET.SubElement(item, "guid").text = target.guid_for(version)
                ET.SubElement(item, "description").text = ""
            ET.ElementTree(root).write(
                feed_path, encoding="utf-8", xml_declaration=True
            )

            for target in (mainland, international):
                result = process_target(
                    session,
                    target,
                    state,
                    directory_path,
                    "软件版本：",
                    "",
                    "gemini-2.5-flash",
                    30,
                    browser,
                    "https://api.day.app",
                    "token",
                    "title",
                    "group",
                )
                self.assertEqual(result, "baseline")

            self.assertEqual(
                set(state["targets"]), {"modely_cn", "modely_com"}
            )
            self.assertEqual(
                (directory_path / "modely-current.pdf").read_bytes(),
                b"%PDF-mainland",
            )
            self.assertEqual(
                (
                    directory_path / "modely-international-current.pdf"
                ).read_bytes(),
                b"%PDF-international",
            )
            self.assertIsNone(pending_rss_entry(feed_path, mainland))
            self.assertIsNone(pending_rss_entry(feed_path, international))
            descriptions = [
                item.findtext("description")
                for item in ET.parse(feed_path).getroot().findall("./channel/item")
            ]
            self.assertTrue(any("Model Y 大陆版" in value for value in descriptions))
            self.assertTrue(any("Model Y 国际版" in value for value in descriptions))

    def test_no_pending_rss_does_not_download_pdf(self):
        session = Mock()
        browser = Mock()
        state = {"schema_version": 2, "targets": {}}
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            target, _ = modely_targets(directory_path / "missing.xml")
            result = process_target(
                session, target, state, directory_path, "软件版本：",
                "", "gemini-2.5-flash", 30, browser,
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
            "schema_version": 2,
            "targets": {
                "modely_cn": {
                    "version": "软件版本：2026.8",
                    "pdf_url": f"{PAGE_URL}Owners_Manual.pdf",
                    "pdf_sha256": "old-hash",
                    "pdf_asset": "modely-current.pdf",
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            asset = directory_path / "modely-current.pdf"
            asset.write_bytes(b"old pdf")
            target, _ = modely_targets(directory_path / "feed.xml")
            pending_feed(
                target.rss_path,
                target,
                version="软件版本：2026.12",
            )
            with patch(
                "scripts.manual_version_monitor.extract_pdf_text",
                side_effect=["old line", "new line"],
            ), patch(
                "scripts.manual_version_monitor.summarize_with_gemini",
                return_value="Gemini summary",
            ):
                result = process_target(
                    session, target, state, directory_path, "软件版本：",
                    "key", "gemini-2.5-flash", 30, browser,
                    "https://api.day.app", "token", "title", "group",
                )

            self.assertEqual(result, "updated")
            self.assertEqual(asset.read_bytes(), b"%PDF-new pdf")
            self.assertEqual(
                state["targets"]["modely_cn"]["version"], "软件版本：2026.12"
            )
            item = ET.parse(target.rss_path).getroot().find("./channel/item")
            self.assertEqual(item.findtext("title"), "大陆版｜软件版本：2026.12")
            self.assertIn("Model Y 大陆版", item.findtext("description"))
            self.assertIn("Gemini summary", item.findtext("description"))
            session.get.assert_called_once()

    def test_bark_failure_does_not_block_rss_or_baseline(self):
        session = Mock()
        session.get.side_effect = __import__("requests").RequestException("failed")
        browser = Mock()
        browser.download.return_value = b"%PDF-fake bytes"
        state = {"schema_version": 2, "targets": {}}
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            target, _ = modely_targets(directory_path / "feed.xml")
            pending_feed(target.rss_path, target)
            result = process_target(
                session, target, state, directory_path, "软件版本：", "",
                "gemini-2.5-flash", 30, browser, "https://api.day.app",
                "token", "title", "group",
            )
            self.assertEqual(result, "baseline")
            self.assertIsNone(pending_rss_entry(target.rss_path, target))
            self.assertNotIn("bark_pending", state["targets"]["modely_cn"])
            self.assertTrue((directory_path / "modely-current.pdf").exists())

    def test_gemini_failure_summary_is_explicit(self):
        summary = fallback_summary("软件版本：1", "软件版本：2", 3, 4)
        self.assertIn("Gemini 总结失败", summary)
        self.assertIn("新增 3 行，删除 4 行", summary)


if __name__ == "__main__":
    unittest.main()
