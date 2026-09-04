import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from scripts.manual_rss_generator import (
    DirectHtmlPageFetcher,
    add_version_entry,
    create_http_session,
    find_version_entry,
    main,
    parse_html_manual_page,
    remove_stale_pending_entries,
    state_matches_version,
    validate_page_pdf_url,
)
from scripts.manual_version_monitor import build_region_targets, manual_pdf_url


MAINLAND_URL = "https://manual.example.cn/owners/model3/zh_cn/index.html"
VERSION_CSS = 'article[role="article"] .body > p.p'
PDF_CSS = 'footer#footer a[href$="Owners_Manual.pdf"]'


def model3_targets(path: Path):
    return build_region_targets(
        "model3",
        "Model 3",
        MAINLAND_URL,
        path,
        "model3-current.pdf",
        "model3-international-current.pdf",
    )


def html_page(version="2026.8", href="./Owners_Manual.pdf") -> bytes:
    return f"""
    <html><body>
      <article role="article"><div class="body">
        <p class="p">Other text</p>
        <p class="p"> software version : <span>{version}</span> </p>
      </div></article>
      <footer id="footer"><a href="{href}">Download PDF</a></footer>
    </body></html>
    """.encode()


def completed_entry(target, version, pdf_url):
    add_version_entry(
        target,
        version,
        pdf_url,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        50,
    )
    tree = ET.parse(target.rss_path)
    item = next(
        item
        for item in tree.getroot().findall("./channel/item")
        if (item.findtext("guid") or "").strip() == target.guid_for(version)
    )
    item.find("description").text = "existing summary"
    tree.write(target.rss_path, encoding="utf-8", xml_declaration=True)


def state_for(targets, versions):
    return {
        "schema_version": 2,
        "targets": {
            target.key: {
                "version": versions[target.key],
                "pdf_url": manual_pdf_url(target.page_url),
                "pdf_sha256": "a" * 64,
                "pdf_asset": target.pdf_asset,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "rss_description": "existing summary",
            }
            for target in targets
        },
    }


def base_environment():
    return {
        "MANUAL_VERSION_CONTAINER_CSS": VERSION_CSS,
        "MANUAL_VERSION_TEXT_PREFIX": "software version:",
        "MANUAL_PDF_LINK_CSS": PDF_CSS,
        "MANUAL_REQUEST_TIMEOUT_SECONDS": "30",
        "MANUAL_RSS_MAX_ITEMS": "50",
        "GITHUB_RUN_ID": "1234",
        "GITHUB_RUN_NUMBER": "56",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_SHA": "abc123",
    }


class HtmlSourceTests(unittest.TestCase):
    def test_html_parser_normalizes_nested_text_and_resolves_pdf(self):
        version, pdf_url = parse_html_manual_page(
            html_page(),
            MAINLAND_URL,
            VERSION_CSS,
            "software version:",
            PDF_CSS,
        )
        self.assertEqual(version, "software version：2026.8")
        self.assertEqual(
            pdf_url,
            "https://manual.example.cn/owners/model3/zh_cn/Owners_Manual.pdf",
        )

    def test_html_parser_requires_exactly_one_version_paragraph(self):
        duplicate = html_page().replace(
            b"</div></article>",
            b'<p class="p">software version: 2026.9</p></div></article>',
        )
        with self.assertRaisesRegex(ValueError, "found 2"):
            parse_html_manual_page(
                duplicate,
                MAINLAND_URL,
                VERSION_CSS,
                "software version:",
                PDF_CSS,
            )

    def test_html_parser_requires_exactly_one_pdf_link(self):
        duplicate = html_page().replace(
            b"</footer>",
            b'<a href="copy/Owners_Manual.pdf">Copy</a></footer>',
        )
        with self.assertRaisesRegex(ValueError, "found 2"):
            parse_html_manual_page(
                duplicate,
                MAINLAND_URL,
                VERSION_CSS,
                "software version:",
                PDF_CSS,
            )

    def test_html_parser_rejects_cross_origin_pdf(self):
        with self.assertRaisesRegex(ValueError, "same origin"):
            parse_html_manual_page(
                html_page(href="https://other.example.cn/Owners_Manual.pdf"),
                MAINLAND_URL,
                VERSION_CSS,
                "software version:",
                PDF_CSS,
            )

    def test_pdf_url_validation_does_not_rewrite_zh_cn_path(self):
        pdf_url = (
            "https://manual.example.cn/owners/model3/zh_cn/Owners_Manual.pdf"
        )
        validate_page_pdf_url(MAINLAND_URL, pdf_url)
        self.assertIn("/zh_cn/", pdf_url)

    def test_html_parser_preserves_same_origin_pdf_query(self):
        version, pdf_url = parse_html_manual_page(
            html_page(href="/downloads/current/Owners_Manual.pdf?cache=2026.8"),
            MAINLAND_URL,
            VERSION_CSS,
            "software version:",
            'footer#footer a[href*="Owners_Manual.pdf"]',
        )
        self.assertEqual(version, "software version：2026.8")
        self.assertEqual(
            pdf_url,
            "https://manual.example.cn/downloads/current/Owners_Manual.pdf?cache=2026.8",
        )

    def test_pdf_url_validation_rejects_fragment(self):
        with self.assertRaisesRegex(ValueError, "fragment"):
            validate_page_pdf_url(
                MAINLAND_URL,
                "https://manual.example.cn/Owners_Manual.pdf#page=1",
            )

    def test_http_session_ignores_runner_proxy_environment(self):
        session = create_http_session()
        try:
            self.assertFalse(session.trust_env)
            self.assertIn("Mozilla/5.0", session.headers["User-Agent"])
        finally:
            session.close()

    def test_direct_fetcher_uses_http_response_bytes(self):
        response = MagicMock(content=html_page())
        session = MagicMock()
        session.get.return_value = response
        fetcher = DirectHtmlPageFetcher(
            17,
            VERSION_CSS,
            "software version:",
            PDF_CSS,
            session=session,
        )
        version, pdf_url = fetcher.fetch(MAINLAND_URL)
        session.get.assert_called_once_with(MAINLAND_URL, timeout=17)
        response.raise_for_status.assert_called_once_with()
        self.assertEqual(version, "software version：2026.8")
        self.assertTrue(pdf_url.endswith("/Owners_Manual.pdf"))


class FeedTests(unittest.TestCase):
    def test_state_alignment_requires_valid_hash_and_regional_pdf_url(self):
        with tempfile.TemporaryDirectory() as directory:
            mainland, international = model3_targets(Path(directory) / "feed.xml")
            version = "software version：2026.8"
            valid = state_for(
                [mainland, international],
                {mainland.key: version, international.key: version},
            )["targets"]
            self.assertTrue(state_matches_version(valid, mainland, version))
            self.assertTrue(state_matches_version(valid, international, version))

            invalid_hash = {key: dict(value) for key, value in valid.items()}
            invalid_hash[mainland.key]["pdf_sha256"] = "A" * 64
            self.assertFalse(
                state_matches_version(invalid_hash, mainland, version)
            )

            wrong_region = {key: dict(value) for key, value in valid.items()}
            wrong_region[mainland.key]["pdf_url"] = manual_pdf_url(
                international.page_url
            )
            self.assertFalse(state_matches_version(wrong_region, mainland, version))

    def test_first_run_creates_blank_region_item(self):
        with tempfile.TemporaryDirectory() as directory:
            target, _ = model3_targets(Path(directory) / "feed.xml")
            changed = add_version_entry(
                target,
                "software version：2026.8",
                "https://manual.example.cn/current.pdf",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                50,
            )
            item = ET.parse(target.rss_path).getroot().find("./channel/item")
            self.assertTrue(changed)
            self.assertEqual(item.findtext("title"), "大陆版｜software version：2026.8")
            self.assertEqual(
                item.findtext("guid"),
                "manual-model3-cn-software version：2026.8",
            )
            self.assertEqual(item.findtext("description"), "")
            self.assertEqual(
                find_version_entry(target, "software version：2026.8").guid,
                "manual-model3-cn-software version：2026.8",
            )

    def test_same_version_creates_distinct_region_items(self):
        with tempfile.TemporaryDirectory() as directory:
            mainland, international = model3_targets(Path(directory) / "feed.xml")
            detected_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            self.assertTrue(
                add_version_entry(
                    mainland,
                    "software version：2026.8",
                    "https://manual.example.cn/current.pdf",
                    detected_at,
                    50,
                )
            )
            self.assertTrue(
                add_version_entry(
                    international,
                    "software version：2026.8",
                    "https://manual.example.com/current.pdf",
                    detected_at,
                    50,
                )
            )
            guids = {
                item.findtext("guid")
                for item in ET.parse(mainland.rss_path).getroot().findall(
                    "./channel/item"
                )
            }
            self.assertEqual(
                guids,
                {
                    "manual-model3-cn-software version：2026.8",
                    "manual-model3-com-software version：2026.8",
                },
            )

    def test_legacy_mainland_guid_prevents_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            mainland, _ = model3_targets(path)
            root = ET.Element("rss", {"version": "2.0"})
            channel = ET.SubElement(root, "channel")
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = "software version：2026.8"
            ET.SubElement(item, "link").text = (
                "https://manual.example.cn/current.pdf"
            )
            ET.SubElement(item, "guid").text = (
                "manual-model3-software version：2026.8"
            )
            ET.SubElement(item, "description").text = "existing summary"
            ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
            self.assertFalse(
                add_version_entry(
                    mainland,
                    "software version：2026.8",
                    "https://manual.example.cn/current.pdf",
                    datetime(2026, 1, 1, tzinfo=timezone.utc),
                    50,
                )
            )
            self.assertEqual(
                find_version_entry(mainland, "software version：2026.8").guid,
                "manual-model3-software version：2026.8",
            )

    def test_removes_only_stale_pending_item_for_same_region(self):
        with tempfile.TemporaryDirectory() as directory:
            mainland, international = model3_targets(Path(directory) / "feed.xml")
            detected_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            add_version_entry(
                mainland,
                "software version：2026.7",
                "https://manual.example.cn/old.pdf",
                detected_at,
                50,
            )
            add_version_entry(
                international,
                "software version：2026.26",
                "https://manual.example.com/current.pdf",
                detected_at,
                50,
            )
            removed = remove_stale_pending_entries(
                mainland, "software version：2026.8"
            )
            self.assertEqual(removed, ["software version：2026.7"])
            remaining = ET.parse(mainland.rss_path).getroot().findall(
                "./channel/item"
            )
            self.assertEqual(len(remaining), 1)
            self.assertIn("-com-", remaining[0].findtext("guid"))


class GeneratorMainTests(unittest.TestCase):
    def run_main(
        self,
        directory: Path,
        targets,
        html_versions,
        pdf_versions,
        state=None,
    ):
        candidate_dir = directory / "candidates"
        result_file = directory / "result.json"
        state_file = directory / "state.json"
        if state is not None:
            state_file.write_text(json.dumps(state), encoding="utf-8")

        html_fetcher = MagicMock()
        html_fetcher.fetch.side_effect = [
            (
                html_versions[target.key],
                target.page_url.rsplit("/", 1)[0] + "/Owners_Manual.pdf",
            )
            for target in targets
            if target.region_code == "cn"
        ]
        html_fetcher.download_pdf.side_effect = lambda url: (
            b"%PDF-" + url.encode("utf-8")
        )
        pdf_fetcher = MagicMock()
        pdf_fetcher.download.side_effect = lambda url: (
            b"%PDF-" + url.encode("utf-8")
        )

        def extract_version(_directory, target, _content, _prefix):
            return pdf_versions[target.key]

        with patch(
            "scripts.manual_rss_generator.configured_targets", return_value=targets
        ), patch(
                "scripts.manual_rss_generator.DirectHtmlPageFetcher",
                return_value=html_fetcher,
        ), patch(
                "scripts.manual_rss_generator.BrowserFetcher",
                return_value=pdf_fetcher,
        ), patch(
                "scripts.manual_rss_generator.extract_downloaded_pdf_version",
                side_effect=extract_version,
        ), patch.dict(os.environ, base_environment(), clear=False):
            arguments = [
                "--result-file",
                str(result_file),
                "--candidate-dir",
                str(candidate_dir),
                "--state-file",
                str(state_file),
            ]
            exit_code = main(arguments)
        return (
            exit_code,
            json.loads(result_file.read_text(encoding="utf-8")),
            json.loads(
                (candidate_dir / "manual-pdf-candidates.json").read_text(
                    encoding="utf-8"
                )
            ),
            html_fetcher,
            pdf_fetcher,
        )

    def test_first_run_generates_four_entries_and_artifact_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model3 = model3_targets(root / "model3.xml")
            modely = build_region_targets(
                "modely",
                "Model Y",
                "https://manual.example.cn/owners/modely/zh_cn/index.html",
                root / "modely.xml",
                "modely-current.pdf",
                "modely-international-current.pdf",
            )
            targets = model3 + modely
            html_versions = {
                "model3_cn": "software version：2026.8",
                "modely_cn": "software version：2026.8",
            }
            pdf_versions = {
                "model3_cn": "software version：2026.8",
                "model3_com": "software version：2026.26",
                "modely_cn": "software version：2026.8",
                "modely_com": "software version：2026.26",
            }
            exit_code, result, manifest, html_fetcher, pdf_fetcher = self.run_main(
                root, targets, html_versions, pdf_versions
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(result["candidate_count"], 4)
            self.assertTrue(result["dispatch_required"])
            self.assertEqual(result["pending_targets"], [t.key for t in targets])
            self.assertEqual([c["target_key"] for c in manifest["candidates"]], [t.key for t in targets])
            self.assertEqual(
                [c["version_source"] for c in manifest["candidates"]],
                ["html+pdf", "pdf", "html+pdf", "pdf"],
            )
            self.assertEqual({c["mode"] for c in manifest["candidates"]}, {"pending"})
            self.assertEqual(
                manifest["source_run"],
                {"id": 1234, "number": 56, "attempt": 2, "commit": "abc123"},
            )
            self.assertEqual(html_fetcher.fetch.call_count, 2)
            self.assertEqual(html_fetcher.download_pdf.call_count, 2)
            self.assertEqual(pdf_fetcher.download.call_count, 2)
            for target in targets:
                self.assertTrue((root / "candidates" / target.pdf_asset).is_file())
                record = next(
                    c for c in manifest["candidates"] if c["target_key"] == target.key
                )
                self.assertEqual(record["candidate_file"], target.pdf_asset)
                self.assertEqual(record["pdf_asset"], target.pdf_asset)
                self.assertEqual(len(record["pdf_sha256"]), 64)
                self.assertGreater(record["pdf_size"], 5)
            self.assertEqual(
                len(ET.parse(root / "model3.xml").getroot().findall("./channel/item")),
                2,
            )
            self.assertEqual(
                len(ET.parse(root / "modely.xml").getroot().findall("./channel/item")),
                2,
            )

    def test_unchanged_mainland_skips_pdf_while_international_reads_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            targets = model3_targets(root / "model3.xml")
            cn, com = targets
            cn_pdf = "https://manual.example.cn/owners/model3/zh_cn/Owners_Manual.pdf"
            com_pdf = "https://manual.example.com/owners/model3/zh_cn/Owners_Manual.pdf"
            completed_entry(cn, "software version：2026.8", cn_pdf)
            completed_entry(com, "software version：2026.26", com_pdf)
            state = state_for(
                targets,
                {
                    "model3_cn": "software version：2026.8",
                    "model3_com": "software version：2026.26",
                },
            )
            exit_code, result, manifest, _html, pdf_fetcher = self.run_main(
                root,
                targets,
                {"model3_cn": "software version：2026.8"},
                {
                    "model3_cn": "software version：2026.8",
                    "model3_com": "software version：2026.26",
                },
                state,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(result["results"], {"model3_cn": "unchanged", "model3_com": "unchanged"})
            self.assertEqual(result["candidate_count"], 0)
            self.assertFalse(result["dispatch_required"])
            self.assertEqual(manifest["candidates"], [])
            self.assertEqual(
                pdf_fetcher.download.call_args_list,
                [call(com_pdf)],
            )
            html_fetcher = _html
            html_fetcher.download_pdf.assert_not_called()

    def test_state_mismatch_creates_reconcile_candidate_without_duplicate_rss(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _ = model3_targets(root / "model3.xml")
            pdf_url = "https://manual.example.cn/owners/model3/zh_cn/Owners_Manual.pdf"
            completed_entry(target, "software version：2026.8", pdf_url)
            wrong_state = state_for(
                [target], {"model3_cn": "software version：2026.26"}
            )
            exit_code, result, manifest, _html, _pdf = self.run_main(
                root,
                [target],
                {"model3_cn": "software version：2026.8"},
                {"model3_cn": "software version：2026.8"},
                wrong_state,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(result["results"]["model3_cn"], "reconcile")
            candidate = manifest["candidates"][0]
            self.assertEqual(candidate["mode"], "reconcile")
            self.assertEqual(candidate["version"], "software version：2026.8")
            self.assertEqual(
                len(ET.parse(target.rss_path).getroot().findall("./channel/item")), 1
            )

    def test_html_pdf_version_mismatch_refuses_rss_and_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _ = model3_targets(root / "model3.xml")
            exit_code, result, manifest, _html, _pdf = self.run_main(
                root,
                [target],
                {"model3_cn": "software version：2026.8"},
                {"model3_cn": "software version：2026.26"},
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(result["results"]["model3_cn"], "failed")
            self.assertIn("does not match", result["errors"][0])
            self.assertEqual(manifest["candidates"], [])
            self.assertFalse(target.rss_path.exists())

    def test_verified_current_version_replaces_stale_pending_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _ = model3_targets(root / "model3.xml")
            add_version_entry(
                target,
                "software version：2026.26",
                "https://manual.example.cn/owners/model3/zh_cn/Owners_Manual.pdf",
                datetime(2026, 1, 1, tzinfo=timezone.utc),
                50,
            )
            exit_code, result, manifest, _html, _pdf = self.run_main(
                root,
                [target],
                {"model3_cn": "software version：2026.8"},
                {"model3_cn": "software version：2026.8"},
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(result["results"]["model3_cn"], "added")
            self.assertEqual(result["pending_targets"], ["model3_cn"])
            items = ET.parse(target.rss_path).getroot().findall("./channel/item")
            self.assertEqual(len(items), 1)
            self.assertEqual(
                items[0].findtext("guid"),
                "manual-model3-cn-software version：2026.8",
            )
            self.assertEqual(manifest["candidates"][0]["mode"], "pending")

    def test_one_target_failure_does_not_discard_other_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            targets = model3_targets(root / "model3.xml")
            exit_code, result, manifest, _html, _pdf = self.run_main(
                root,
                targets,
                {"model3_cn": "software version：2026.8"},
                {
                    "model3_cn": "software version：2026.9",
                    "model3_com": "software version：2026.26",
                },
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(result["results"]["model3_cn"], "failed")
            self.assertEqual(result["results"]["model3_com"], "added")
            self.assertEqual([c["target_key"] for c in manifest["candidates"]], ["model3_com"])

    def test_missing_css_secret_is_configuration_error_without_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_file = root / "result.json"
            environment = base_environment()
            environment.pop("MANUAL_VERSION_CONTAINER_CSS")
            with patch.dict(os.environ, environment, clear=True), patch(
                "scripts.manual_rss_generator.BrowserFetcher"
            ) as browser:
                exit_code = main(
                    [
                        "--result-file",
                        str(result_file),
                        "--candidate-dir",
                        str(root / "candidates"),
                    ]
                )
            self.assertEqual(exit_code, 1)
            self.assertIn(
                "MANUAL_VERSION_CONTAINER_CSS",
                json.loads(result_file.read_text(encoding="utf-8"))["errors"][0],
            )
            browser.assert_not_called()

    def test_source_contains_no_tavily_dependency(self):
        source = (Path(__file__).parents[1] / "scripts/manual_rss_generator.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Tavily", source)
        self.assertNotIn("TAVILY_API_KEY", source)


if __name__ == "__main__":
    unittest.main()
