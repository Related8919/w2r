import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

import requests

from scripts.miit_vehicle_monitor import (
    build_bark_url,
    extract_docx_text,
    find_attachments,
    new_article_urls,
    send_bark,
)


def feed(items):
    entries = "".join(
        f"<item><title>{title}</title><link>{link}</link></item>"
        for title, link in items
    )
    return f"<rss><channel>{entries}</channel></rss>".encode()


class MiitVehicleMonitorTests(unittest.TestCase):
    def test_new_article_urls_filters_titles_and_previous_links(self):
        keyword = "道路机动车辆生产企业及产品"
        previous = feed([(f"{keyword}（第406批）", "https://example/406")])
        current = feed(
            [
                (f"{keyword}（第407批）", "https://example/407"),
                (f"{keyword}（第406批）", "https://example/406"),
                ("正文中出现关键字的其他文章", "https://example/other"),
            ]
        )

        self.assertEqual(
            new_article_urls(current, previous, keyword),
            ["https://example/407"],
        )

    def test_find_attachments_keeps_matching_doc_only(self):
        html = '''<div id="con_con">
          <a href="/attach/vehicle.doc">1.道路机动车辆生产企业及产品.doc</a>
          <a href="/attach/tax.doc">2.车船税目录.doc</a>
          <a href="/attach/vehicle.pdf">3.道路机动车辆生产企业及产品.pdf</a>
        </div>'''.encode("utf-8")

        self.assertEqual(
            find_attachments(
                html,
                "https://www.miit.gov.cn/article.html",
                "道路机动车辆生产企业及产品",
                "www.miit.gov.cn",
            ),
            [
                (
                    "1.道路机动车辆生产企业及产品.doc",
                    "https://www.miit.gov.cn/attach/vehicle.doc",
                )
            ],
        )

    def test_extract_docx_text_reads_word_xml(self):
        document_xml = b'''<?xml version="1.0" encoding="UTF-8"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>contains xxx here</w:t></w:r></w:p></w:body>
        </w:document>'''
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "sample.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)

            self.assertIn("xxx", extract_docx_text(path))

    def test_bark_token_is_encoded_in_url_path(self):
        url = build_bark_url(
            "https://api.day.app",
            "secret/token",
            "title",
            "body",
            "https://www.miit.gov.cn/article.html",
            "group",
        )

        self.assertIn("/secret%2Ftoken/title/body?", url)
        self.assertIn("url=https%3A%2F%2Fwww.miit.gov.cn%2Farticle.html", url)

    def test_bark_request_error_does_not_expose_request_url(self):
        session = Mock()
        session.get.side_effect = requests.RequestException(
            "failed https://api.day.app/secret-token/title/body"
        )

        with self.assertRaisesRegex(RuntimeError, "Bark request failed") as context:
            send_bark(
                session,
                "https://api.day.app",
                "secret-token",
                "title",
                "body",
                "https://www.miit.gov.cn/article.html",
                "group",
                30,
            )

        self.assertNotIn("secret-token", str(context.exception))


if __name__ == "__main__":
    unittest.main()
