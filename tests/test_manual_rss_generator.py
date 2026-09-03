import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.manual_rss_generator import add_version_entry, main
from scripts.manual_version_monitor import build_region_targets


MAINLAND_URL = "https://manual.example.cn/owners/model3/zh_cn/index.html"


def model3_targets(path: Path):
    return build_region_targets(
        "model3",
        "Model 3",
        MAINLAND_URL,
        path,
        "model3-current.pdf",
        "model3-international-current.pdf",
    )


class ManualRssGeneratorTests(unittest.TestCase):
    @patch("scripts.manual_rss_generator.BrowserFetcher")
    def test_main_generates_four_pending_targets_from_two_mainland_urls(
        self, browser_fetcher
    ):
        html = b'''<article role="article"><div class="body">
          <p class="p">software version: 2026.8</p>
        </div></article>
        <footer id="footer"><a href="Owners_Manual.pdf">PDF</a></footer>'''
        browser_fetcher.return_value.page_html.return_value = html

        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            model3_feed = directory_path / "model3.xml"
            modely_feed = directory_path / "modely.xml"
            result_file = directory_path / "result.json"
            environment = {
                "MODEL3_MANUAL_URL": "https://manual.example.cn/model3/zh_cn/",
                "MODELY_MANUAL_URL": "https://manual.example.cn/modely/zh_cn/",
                "MODEL3_MANUAL_RSS_PATH": str(model3_feed),
                "MODELY_MANUAL_RSS_PATH": str(modely_feed),
                "MANUAL_VERSION_CONTAINER_CSS": (
                    'article[role="article"] .body > p.p'
                ),
                "MANUAL_VERSION_TEXT_PREFIX": "software version:",
                "MANUAL_PDF_LINK_CSS": (
                    'footer#footer a[href$="Owners_Manual.pdf"]'
                ),
                "MANUAL_RSS_MAX_ITEMS": "50",
            }
            with patch.dict(os.environ, environment, clear=False):
                exit_code = main(["--result-file", str(result_file)])

            self.assertEqual(exit_code, 0)
            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(
                result["pending_targets"],
                ["model3_cn", "model3_com", "modely_cn", "modely_com"],
            )
            self.assertEqual(
                len(ET.parse(model3_feed).getroot().findall("./channel/item")), 2
            )
            self.assertEqual(
                len(ET.parse(modely_feed).getroot().findall("./channel/item")), 2
            )
            called_urls = [
                call.args[0]
                for call in browser_fetcher.return_value.page_html.call_args_list
            ]
            self.assertEqual(
                called_urls,
                [
                    "https://manual.example.cn/model3/zh_cn/",
                    "https://manual.example.com/model3/zh_cn/",
                    "https://manual.example.cn/modely/zh_cn/",
                    "https://manual.example.com/modely/zh_cn/",
                ],
            )

    @patch("scripts.manual_rss_generator.BrowserFetcher")
    def test_main_keeps_successful_regions_when_one_region_fails(
        self, browser_fetcher
    ):
        html = b'''<article role="article"><div class="body">
          <p class="p">software version: 2026.8</p>
        </div></article>
        <footer id="footer"><a href="Owners_Manual.pdf">PDF</a></footer>'''

        def page_html(url, _prefix):
            if "manual.example.com/model3" in url:
                raise RuntimeError("Access Denied")
            return html

        browser_fetcher.return_value.page_html.side_effect = page_html
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            model3_feed = directory_path / "model3.xml"
            modely_feed = directory_path / "modely.xml"
            result_file = directory_path / "result.json"
            environment = {
                "MODEL3_MANUAL_URL": "https://manual.example.cn/model3/zh_cn/",
                "MODELY_MANUAL_URL": "https://manual.example.cn/modely/zh_cn/",
                "MODEL3_MANUAL_RSS_PATH": str(model3_feed),
                "MODELY_MANUAL_RSS_PATH": str(modely_feed),
                "MANUAL_VERSION_CONTAINER_CSS": (
                    'article[role="article"] .body > p.p'
                ),
                "MANUAL_VERSION_TEXT_PREFIX": "software version:",
                "MANUAL_PDF_LINK_CSS": (
                    'footer#footer a[href$="Owners_Manual.pdf"]'
                ),
            }
            with patch.dict(os.environ, environment, clear=False):
                exit_code = main(["--result-file", str(result_file)])

            result = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 1)
            self.assertEqual(result["results"]["model3_com"], "failed")
            self.assertEqual(
                result["pending_targets"],
                ["model3_cn", "modely_cn", "modely_com"],
            )
            self.assertEqual(
                len(ET.parse(model3_feed).getroot().findall("./channel/item")), 1
            )
            self.assertEqual(
                len(ET.parse(modely_feed).getroot().findall("./channel/item")), 2
            )

    def test_first_run_creates_blank_version_item(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            target, _ = model3_targets(path)
            changed = add_version_entry(
                target,
                "软件版本：2026.8",
                "https://example.com/manual.pdf",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                50,
            )
            item = ET.parse(path).getroot().find("./channel/item")
            self.assertTrue(changed)
            self.assertEqual(item.findtext("title"), "大陆版｜软件版本：2026.8")
            self.assertEqual(
                item.findtext("guid"), "manual-model3-cn-软件版本：2026.8"
            )
            self.assertEqual(item.findtext("description"), "")

    def test_existing_version_is_not_added_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            target, _ = model3_targets(path)
            arguments = (
                target,
                "软件版本：2026.8",
                "https://example.com/manual.pdf",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                50,
            )
            self.assertTrue(add_version_entry(*arguments))
            self.assertFalse(add_version_entry(*arguments))
            self.assertEqual(
                len(ET.parse(path).getroot().findall("./channel/item")), 1
            )

    def test_feed_keeps_configured_item_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            target, _ = model3_targets(path)
            for number in range(4):
                add_version_entry(
                    target,
                    f"软件版本：{number}",
                    "https://example.com/manual.pdf",
                    datetime(2026, 1, number + 1, tzinfo=timezone.utc),
                    3,
                )
            items = ET.parse(path).getroot().findall("./channel/item")
            self.assertEqual(len(items), 3)
            self.assertEqual(items[0].findtext("title"), "大陆版｜软件版本：3")

    def test_same_version_creates_separate_region_items(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            mainland, international = model3_targets(path)
            detected_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

            self.assertTrue(
                add_version_entry(
                    mainland,
                    "软件版本：2026.8",
                    "https://manual.example.cn/current.pdf",
                    detected_at,
                    50,
                )
            )
            self.assertTrue(
                add_version_entry(
                    international,
                    "软件版本：2026.8",
                    "https://manual.example.com/current.pdf",
                    detected_at,
                    50,
                )
            )

            items = ET.parse(path).getroot().findall("./channel/item")
            self.assertEqual(len(items), 2)
            self.assertEqual(
                {item.findtext("title") for item in items},
                {
                    "大陆版｜软件版本：2026.8",
                    "国际版｜软件版本：2026.8",
                },
            )
            self.assertEqual(
                {item.findtext("guid") for item in items},
                {
                    "manual-model3-cn-软件版本：2026.8",
                    "manual-model3-com-软件版本：2026.8",
                },
            )

    def test_legacy_mainland_guid_prevents_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            mainland, _ = model3_targets(path)
            root = ET.Element("rss", {"version": "2.0"})
            channel = ET.SubElement(root, "channel")
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = "软件版本：2026.8"
            ET.SubElement(item, "link").text = "https://manual.example.cn/current.pdf"
            ET.SubElement(item, "guid").text = "manual-model3-软件版本：2026.8"
            ET.SubElement(item, "description").text = "existing summary"
            ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)

            self.assertFalse(
                add_version_entry(
                    mainland,
                    "软件版本：2026.8",
                    "https://manual.example.cn/current.pdf",
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    50,
                )
            )
            self.assertEqual(
                len(ET.parse(path).getroot().findall("./channel/item")), 1
            )


if __name__ == "__main__":
    unittest.main()
