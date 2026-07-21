import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from scripts.manual_rss_generator import add_version_entry
from scripts.manual_version_monitor import Target


class ManualRssGeneratorTests(unittest.TestCase):
    def test_first_run_creates_blank_version_item(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            target = Target(
                "model3",
                "Model 3",
                "https://example.com/manual/",
                path,
                "current.pdf",
            )
            changed = add_version_entry(
                target,
                "软件版本：2026.8",
                "https://example.com/manual.pdf",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                50,
            )
            item = ET.parse(path).getroot().find("./channel/item")
            self.assertTrue(changed)
            self.assertEqual(item.findtext("title"), "软件版本：2026.8")
            self.assertEqual(item.findtext("description"), "")

    def test_existing_version_is_not_added_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            target = Target(
                "model3",
                "Model 3",
                "https://example.com/manual/",
                path,
                "current.pdf",
            )
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
            target = Target(
                "model3",
                "Model 3",
                "https://example.com/manual/",
                path,
                "current.pdf",
            )
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
            self.assertEqual(items[0].findtext("title"), "软件版本：3")


if __name__ == "__main__":
    unittest.main()
