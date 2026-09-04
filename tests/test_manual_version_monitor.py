import hashlib
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.manual_version_monitor import (
    BrowserFetcher,
    CANDIDATE_MANIFEST_NAME,
    SourceRun,
    build_region_targets,
    derive_international_url,
    extract_pdf_cover_version,
    extract_pdf_text,
    fallback_summary,
    load_candidate_manifest,
    load_state,
    make_text_diff,
    manual_pdf_url,
    pending_rss_entry,
    process_target,
    rss_entry_by_guid,
    send_bark,
    summarize_with_gemini,
    validate_candidate,
    write_monitor_result,
)


PAGE_URL = "https://manual.example.cn/manual/modely/index.html"
VERSION_PREFIX = "Version："
VERSION_OLD = "Version：2026.8"
VERSION_NEW = "Version：2026.12"
SOURCE_RUN = SourceRun(1001, 50, 1, "abc123")


def simple_pdf(version="2026.8", marker="manual"):
    text = f"Owner Manual Version: {version} {marker}"
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(value)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(output)


def modely_targets(path):
    return build_region_targets(
        "modely",
        "Model Y",
        PAGE_URL,
        path,
        "modely-current.pdf",
        "modely-international-current.pdf",
    )


def write_feed(path, entries):
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    for entry in entries:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = entry["title"]
        ET.SubElement(item, "link").text = entry["link"]
        ET.SubElement(item, "guid").text = entry["guid"]
        ET.SubElement(item, "description").text = entry.get("description", "")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def feed_entry(target, version=VERSION_OLD, description="", legacy=False):
    return {
        "title": version if legacy else target.item_title(version),
        "link": manual_pdf_url(target.page_url),
        "guid": (
            f"{target.legacy_guid_prefix}{version}"
            if legacy
            else target.guid_for(version)
        ),
        "description": description,
    }


def candidate_entry(
    directory,
    target,
    version=VERSION_OLD,
    pdf_bytes=None,
    mode="pending",
    rss_guid=None,
):
    pdf_bytes = pdf_bytes or simple_pdf(version.rsplit("：", 1)[1])
    (directory / target.pdf_asset).write_bytes(pdf_bytes)
    return {
        "target_key": target.key,
        "version": version,
        "pdf_url": manual_pdf_url(target.page_url),
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "pdf_size": len(pdf_bytes),
        "pdf_asset": target.pdf_asset,
        "candidate_file": target.pdf_asset,
        "version_source": "html+pdf" if target.region_code == "cn" else "pdf",
        "rss_guid": rss_guid or target.guid_for(version),
        "mode": mode,
    }


def write_manifest(directory, entries, source_run=SOURCE_RUN):
    value = {
        "schema_version": 1,
        "source_run": {
            "id": source_run.run_id,
            "number": source_run.number,
            "attempt": source_run.attempt,
            "commit": source_run.commit,
        },
        "candidates": entries,
    }
    (directory / CANDIDATE_MANIFEST_NAME).write_text(
        json.dumps(value), encoding="utf-8"
    )


def installed_state(target, pdf_bytes, version=VERSION_OLD, source_run=None):
    value = {
        "version": version,
        "pdf_url": manual_pdf_url(target.page_url),
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "pdf_size": len(pdf_bytes),
        "pdf_asset": target.pdf_asset,
        "rss_description": "existing summary",
    }
    if source_run:
        value["source_run"] = source_run.as_state()
    return value


def successful_session():
    session = Mock()
    response = Mock(status_code=200)
    response.json.return_value = {"code": 200}
    session.get.return_value = response
    return session


def run_target(session, target, state, state_dir, candidate):
    return process_target(
        session,
        target,
        state,
        state_dir,
        candidate,
        SOURCE_RUN,
        VERSION_PREFIX,
        "key",
        "gemini-2.5-flash",
        30,
        "https://api.day.app",
        "token",
        "title",
        "group",
    )


class ManualVersionMonitorTests(unittest.TestCase):
    def test_url_derivation_changes_only_hostname_suffix(self):
        mainland = "https://www.example.cn:8443/owners/zh_cn/?region=cn#cn"
        self.assertEqual(
            derive_international_url(mainland),
            "https://www.example.com:8443/owners/zh_cn/?region=cn#cn",
        )
        for invalid in (
            "ftp://www.example.cn/manual",
            "https:///manual",
            "https://www.example.com/manual",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                derive_international_url(invalid)

    def test_pdf_cover_version_is_read_from_real_pdf_text_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.pdf"
            path.write_bytes(simple_pdf("2026.26"))
            self.assertEqual(
                extract_pdf_cover_version(path, VERSION_PREFIX),
                "Version：2026.26",
            )
            self.assertIn("Version: 2026.26", extract_pdf_text(path))
            path.write_bytes(simple_pdf("2026.8", "Version: 2026.12"))
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                extract_pdf_cover_version(path, VERSION_PREFIX)
            path.write_bytes(simple_pdf("2026.8", "Version: 2026.8"))
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                extract_pdf_cover_version(path, VERSION_PREFIX)

    def test_diff_counts_changes_and_handles_no_text(self):
        diff, additions, deletions = make_text_diff("one\ntwo", "one\nthree")
        self.assertIn("+three", diff)
        self.assertEqual((additions, deletions), (1, 1))
        diff, additions, deletions = make_text_diff("", "")
        self.assertIn("无可提取的文本层", diff)
        self.assertEqual((additions, deletions), (0, 0))
        diff, additions, deletions = make_text_diff("", "new text")
        self.assertIn("上一版本 PDF 无可提取", diff)
        self.assertEqual((additions, deletions), (1, 0))
        diff, additions, deletions = make_text_diff("old text", "")
        self.assertIn("当前版本 PDF 无可提取", diff)
        self.assertEqual((additions, deletions), (0, 1))

    def test_extract_pdf_text_returns_empty_when_every_page_is_blank(self):
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            with path.open("wb") as output:
                writer.write(output)
            self.assertEqual(extract_pdf_text(path), "")

    def test_browser_fetcher_remains_shared_for_producer_and_demo(self):
        driver = Mock()
        download_directory = None

        def execute_cdp(command, params):
            nonlocal download_directory
            if command == "Browser.setDownloadBehavior":
                download_directory = Path(params["downloadPath"])
                return {}
            if command == "Page.navigate":
                (download_directory / "Owners_Manual.pdf").write_bytes(b"%PDF-test")
                return {"frameId": "frame"}
            raise AssertionError(command)

        driver.execute_cdp_cmd.side_effect = execute_cdp
        driver.get_log.return_value = []
        fetcher = BrowserFetcher(1)
        fetcher.driver = driver
        self.assertEqual(fetcher.download(manual_pdf_url(PAGE_URL)), b"%PDF-test")

    def test_new_mainland_guid_is_not_mistaken_for_legacy_prefix(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            mainland, _ = modely_targets(path)
            write_feed(path, [feed_entry(mainland)])
            self.assertEqual(
                pending_rss_entry(path, mainland),
                (
                    VERSION_OLD,
                    manual_pdf_url(mainland.page_url),
                    mainland.guid_for(VERSION_OLD),
                ),
            )

    def test_pending_entries_and_completion_are_region_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            mainland, international = modely_targets(path)
            write_feed(path, [feed_entry(international), feed_entry(mainland)])
            self.assertEqual(
                pending_rss_entry(path, mainland)[2], mainland.guid_for(VERSION_OLD)
            )
            self.assertEqual(
                pending_rss_entry(path, international)[2],
                international.guid_for(VERSION_OLD),
            )

    def test_load_state_migrates_legacy_mainland_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            legacy = {"version": VERSION_OLD, "pdf_asset": "old.pdf"}
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "targets": {"model3": legacy, "modely": legacy},
                    }
                ),
                encoding="utf-8",
            )
            state = load_state(path)
            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(state["targets"]["model3_cn"], legacy)
            self.assertEqual(state["targets"]["modely_cn"], legacy)

    def test_manifest_validates_source_metadata_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _ = modely_targets(root / "feed.xml")
            entry = candidate_entry(root, target)
            write_manifest(root, [entry])
            environment = {
                "MANUAL_SOURCE_RUN_ID": "1001",
                "MANUAL_SOURCE_RUN_NUMBER": "50",
                "MANUAL_SOURCE_RUN_ATTEMPT": "1",
                "MANUAL_SOURCE_COMMIT": "abc123",
            }
            with patch.dict(os.environ, environment, clear=False):
                source, candidates = load_candidate_manifest(root)
            self.assertEqual(source, SOURCE_RUN)
            self.assertEqual(set(candidates), {"modely_cn"})
            write_manifest(root, [entry, entry])
            with self.assertRaisesRegex(RuntimeError, "Duplicate candidate"):
                load_candidate_manifest(root)
            write_manifest(root, [entry])
            with patch.dict(
                os.environ, {"MANUAL_SOURCE_RUN_ID": "999"}, clear=False
            ), self.assertRaisesRegex(RuntimeError, "MANUAL_SOURCE_RUN_ID"):
                load_candidate_manifest(root)

    def test_candidate_strictly_validates_artifact_metadata(self):
        mutations = {
            "target": {"target_key": "modely_com"},
            "guid": {"rss_guid": "manual-modely-com-Version：2026.8"},
            "url": {
                "pdf_url": "https://manual.example.com/manual/modely/Owners_Manual.pdf"
            },
            "asset": {"pdf_asset": "other.pdf"},
            "filename": {"candidate_file": "../modely-current.pdf"},
            "source": {"version_source": "pdf"},
            "sha": {"pdf_sha256": "0" * 64},
            "size": {"pdf_size": 1},
            "mode": {"mode": "other"},
        }
        for name, mutation in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target, _ = modely_targets(root / "feed.xml")
                entry = candidate_entry(root, target)
                entry.update(mutation)
                with self.assertRaises(RuntimeError):
                    validate_candidate(root, entry, target, VERSION_PREFIX)

    def test_candidate_rejects_non_pdf_and_wrong_cover_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, _ = modely_targets(root / "feed.xml")
            entry = candidate_entry(root, target)
            bad = b"not a pdf"
            (root / target.pdf_asset).write_bytes(bad)
            entry.update(
                pdf_size=len(bad), pdf_sha256=hashlib.sha256(bad).hexdigest()
            )
            with self.assertRaisesRegex(RuntimeError, "not a PDF"):
                validate_candidate(root, entry, target, VERSION_PREFIX)
            entry = candidate_entry(root, target, VERSION_OLD, simple_pdf("2026.12"))
            with self.assertRaisesRegex(RuntimeError, "does not match manifest"):
                validate_candidate(root, entry, target, VERSION_PREFIX)

    def test_mainland_candidate_accepts_html_pdf_path_and_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, international = modely_targets(root / "feed.xml")
            entry = candidate_entry(root, target)
            entry["pdf_url"] = (
                "https://manual.example.cn/downloads/current/"
                "Owners_Manual.pdf?cache=2026.8"
            )
            candidate = validate_candidate(root, entry, target, VERSION_PREFIX)
            self.assertEqual(candidate.pdf_url, entry["pdf_url"])

            international_entry = candidate_entry(root, international)
            international_entry["pdf_url"] += "?cache=2026.8"
            with self.assertRaisesRegex(RuntimeError, "does not match its region"):
                validate_candidate(
                    root, international_entry, international, VERSION_PREFIX
                )

    def test_first_run_uses_artifact_without_second_download(self):
        state = {"schema_version": 2, "targets": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "candidate"
            state_dir.mkdir()
            candidate_dir.mkdir()
            target, _ = modely_targets(root / "feed.xml")
            write_feed(target.rss_path, [feed_entry(target)])
            raw = candidate_entry(candidate_dir, target)
            candidate = validate_candidate(candidate_dir, raw, target, VERSION_PREFIX)
            with patch.object(
                BrowserFetcher, "download", side_effect=AssertionError("downloaded")
            ):
                result = run_target(
                    successful_session(), target, state, state_dir, candidate
                )
            self.assertEqual(result, "baseline")
            self.assertEqual(
                (state_dir / target.pdf_asset).read_bytes(), candidate.path.read_bytes()
            )
            self.assertEqual(
                state["targets"][target.key]["source_run"], SOURCE_RUN.as_state()
            )

    def test_two_regions_create_independent_baselines(self):
        state = {"schema_version": 2, "targets": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "candidate"
            state_dir.mkdir()
            candidate_dir.mkdir()
            mainland, international = modely_targets(root / "feed.xml")
            write_feed(mainland.rss_path, [feed_entry(mainland), feed_entry(international)])
            for target, marker in (
                (mainland, "mainland"),
                (international, "international"),
            ):
                raw = candidate_entry(
                    candidate_dir, target, pdf_bytes=simple_pdf("2026.8", marker)
                )
                candidate = validate_candidate(
                    candidate_dir, raw, target, VERSION_PREFIX
                )
                self.assertEqual(
                    run_target(successful_session(), target, state, state_dir, candidate),
                    "baseline",
                )
            self.assertEqual(set(state["targets"]), {"modely_cn", "modely_com"})
            self.assertIn(b"mainland", (state_dir / mainland.pdf_asset).read_bytes())
            self.assertIn(
                b"international", (state_dir / international.pdf_asset).read_bytes()
            )

    def test_update_diffs_only_same_region_release_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "candidate"
            state_dir.mkdir()
            candidate_dir.mkdir()
            mainland, international = modely_targets(root / "feed.xml")
            old_cn = simple_pdf("2026.8", "old-mainland")
            old_com = simple_pdf("2026.8", "old-international")
            (state_dir / mainland.pdf_asset).write_bytes(old_cn)
            (state_dir / international.pdf_asset).write_bytes(old_com)
            state = {
                "schema_version": 2,
                "targets": {
                    mainland.key: installed_state(mainland, old_cn),
                    international.key: installed_state(international, old_com),
                },
            }
            write_feed(mainland.rss_path, [feed_entry(mainland, VERSION_NEW)])
            raw = candidate_entry(candidate_dir, mainland, VERSION_NEW)
            candidate = validate_candidate(candidate_dir, raw, mainland, VERSION_PREFIX)
            observed = []

            def extract(path):
                observed.append(Path(path))
                return Path(path).name

            with patch(
                "scripts.manual_version_monitor.extract_pdf_text", side_effect=extract
            ), patch(
                "scripts.manual_version_monitor.summarize_with_gemini",
                return_value="Gemini summary",
            ):
                result = run_target(
                    successful_session(), mainland, state, state_dir, candidate
                )
            self.assertEqual(result, "updated")
            self.assertEqual(observed, [state_dir / mainland.pdf_asset, candidate.path])
            self.assertNotIn(state_dir / international.pdf_asset, observed)

    def test_gemini_failure_still_saves_baseline_and_rss_description(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "candidate"
            state_dir.mkdir()
            candidate_dir.mkdir()
            target, _ = modely_targets(root / "feed.xml")
            old = simple_pdf("2026.8", "old")
            (state_dir / target.pdf_asset).write_bytes(old)
            state = {
                "schema_version": 2,
                "targets": {target.key: installed_state(target, old)},
            }
            write_feed(target.rss_path, [feed_entry(target, VERSION_NEW)])
            raw = candidate_entry(candidate_dir, target, VERSION_NEW)
            candidate = validate_candidate(candidate_dir, raw, target, VERSION_PREFIX)
            with patch(
                "scripts.manual_version_monitor.summarize_with_gemini",
                side_effect=RuntimeError("Gemini failed"),
            ):
                result = run_target(
                    successful_session(), target, state, state_dir, candidate
                )
            self.assertEqual(result, "updated")
            description = rss_entry_by_guid(
                target.rss_path, target.guid_for(VERSION_NEW)
            ).description
            self.assertIn("Gemini 总结失败", description)
            self.assertEqual(
                (state_dir / target.pdf_asset).read_bytes(), candidate.path.read_bytes()
            )

    def test_bark_failure_does_not_block_baseline(self):
        session = Mock()
        session.get.side_effect = __import__("requests").RequestException("failed")
        state = {"schema_version": 2, "targets": {}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "candidate"
            state_dir.mkdir()
            candidate_dir.mkdir()
            target, _ = modely_targets(root / "feed.xml")
            write_feed(target.rss_path, [feed_entry(target)])
            raw = candidate_entry(candidate_dir, target)
            candidate = validate_candidate(candidate_dir, raw, target, VERSION_PREFIX)
            self.assertEqual(
                run_target(session, target, state, state_dir, candidate), "baseline"
            )
            self.assertTrue((state_dir / target.pdf_asset).exists())
            self.assertNotIn("bark_pending", state["targets"][target.key])

    def test_reconcile_repairs_polluted_state_without_gemini_or_bark(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "candidate"
            state_dir.mkdir()
            candidate_dir.mkdir()
            target, _ = modely_targets(root / "feed.xml")
            actual = simple_pdf("2026.8", "actual-mainland")
            (state_dir / target.pdf_asset).write_bytes(actual)
            state = {
                "schema_version": 2,
                "targets": {
                    target.key: installed_state(target, actual, "Version：2026.26")
                },
            }
            legacy = feed_entry(
                target, VERSION_OLD, "existing completed summary", legacy=True
            )
            write_feed(target.rss_path, [legacy])
            raw = candidate_entry(
                candidate_dir,
                target,
                VERSION_OLD,
                actual,
                mode="reconcile",
                rss_guid=legacy["guid"],
            )
            candidate = validate_candidate(candidate_dir, raw, target, VERSION_PREFIX)
            session = Mock()
            with patch(
                "scripts.manual_version_monitor.summarize_with_gemini"
            ) as gemini:
                result = run_target(session, target, state, state_dir, candidate)
            self.assertEqual(result, "reconciled")
            self.assertEqual(state["targets"][target.key]["version"], VERSION_OLD)
            self.assertEqual(
                rss_entry_by_guid(target.rss_path, legacy["guid"]).description,
                "existing completed summary",
            )
            gemini.assert_not_called()
            session.get.assert_not_called()

    def test_reconcile_refuses_pending_rss_and_preserves_old_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "candidate"
            state_dir.mkdir()
            candidate_dir.mkdir()
            target, _ = modely_targets(root / "feed.xml")
            old = simple_pdf("2026.26", "old")
            (state_dir / target.pdf_asset).write_bytes(old)
            state = {
                "schema_version": 2,
                "targets": {
                    target.key: installed_state(target, old, "Version：2026.26")
                },
            }
            write_feed(target.rss_path, [feed_entry(target)])
            raw = candidate_entry(candidate_dir, target, mode="reconcile")
            candidate = validate_candidate(candidate_dir, raw, target, VERSION_PREFIX)
            with self.assertRaisesRegex(RuntimeError, "must already be complete"):
                run_target(successful_session(), target, state, state_dir, candidate)
            self.assertEqual((state_dir / target.pdf_asset).read_bytes(), old)

    def test_completed_pending_artifact_is_idempotent_only_when_state_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "candidate"
            state_dir.mkdir()
            candidate_dir.mkdir()
            target, _ = modely_targets(root / "feed.xml")
            current = simple_pdf("2026.8")
            (state_dir / target.pdf_asset).write_bytes(current)
            state = {
                "schema_version": 2,
                "targets": {
                    target.key: installed_state(
                        target, current, source_run=SOURCE_RUN
                    )
                },
            }
            write_feed(
                target.rss_path,
                [feed_entry(target, description="already complete")],
            )
            raw = candidate_entry(candidate_dir, target, pdf_bytes=current)
            candidate = validate_candidate(candidate_dir, raw, target, VERSION_PREFIX)
            self.assertEqual(
                run_target(Mock(), target, state, state_dir, candidate),
                "already_processed",
            )
            state["targets"][target.key]["pdf_sha256"] = "0" * 64
            with self.assertRaisesRegex(RuntimeError, "use reconcile mode"):
                run_target(Mock(), target, state, state_dir, candidate)

    def test_pending_recovery_validates_artifact_then_fills_description(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "candidate"
            state_dir.mkdir()
            candidate_dir.mkdir()
            target, _ = modely_targets(root / "feed.xml")
            current = simple_pdf("2026.8")
            (state_dir / target.pdf_asset).write_bytes(current)
            state = {
                "schema_version": 2,
                "targets": {target.key: installed_state(target, current)},
            }
            write_feed(target.rss_path, [feed_entry(target)])
            raw = candidate_entry(candidate_dir, target, pdf_bytes=current)
            candidate = validate_candidate(candidate_dir, raw, target, VERSION_PREFIX)
            self.assertEqual(
                run_target(Mock(), target, state, state_dir, candidate), "recovered"
            )
            self.assertTrue(
                rss_entry_by_guid(
                    target.rss_path, target.guid_for(VERSION_OLD)
                ).description
            )

    def test_older_source_run_cannot_overwrite_newer_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir, candidate_dir = root / "state", root / "candidate"
            state_dir.mkdir()
            candidate_dir.mkdir()
            target, _ = modely_targets(root / "feed.xml")
            old = simple_pdf("2026.8", "newer-run-baseline")
            (state_dir / target.pdf_asset).write_bytes(old)
            newer = SourceRun(1002, 51, 1, "newer")
            state = {
                "schema_version": 2,
                "targets": {
                    target.key: installed_state(target, old, source_run=newer)
                },
            }
            write_feed(target.rss_path, [feed_entry(target, VERSION_NEW)])
            raw = candidate_entry(candidate_dir, target, VERSION_NEW)
            candidate = validate_candidate(candidate_dir, raw, target, VERSION_PREFIX)
            with self.assertRaisesRegex(RuntimeError, "older"):
                run_target(successful_session(), target, state, state_dir, candidate)
            self.assertEqual((state_dir / target.pdf_asset).read_bytes(), old)

    def test_fallback_summary_is_explicit(self):
        summary = fallback_summary("Version：1", "Version：2", 3, 4)
        self.assertIn("Gemini 总结失败", summary)
        self.assertIn("新增 3 行，删除 4 行", summary)

    def test_malformed_service_json_becomes_controlled_runtime_error(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = []
        session = Mock()
        session.get.return_value = response
        session.post.return_value = response

        with self.assertRaisesRegex(RuntimeError, "Bark returned an invalid JSON"):
            send_bark(
                session,
                "https://api.day.app",
                "token",
                "title",
                "body",
                "https://manual.example.cn/manual.pdf",
                "group",
                30,
            )
        with self.assertRaisesRegex(RuntimeError, "Gemini summary failed"):
            summarize_with_gemini(
                session,
                "key",
                "gemini-2.5-flash",
                "Model Y 大陆版",
                VERSION_OLD,
                VERSION_NEW,
                "-old\n+new",
                1,
                1,
                30,
            )

    def test_monitor_result_records_state_save_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result.json"
            write_monitor_result(path, {"modely_cn": "updated"}, [], True)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {
                    "results": {"modely_cn": "updated"},
                    "errors": [],
                    "state_saved": True,
                },
            )


if __name__ == "__main__":
    unittest.main()
