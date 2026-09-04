#!/usr/bin/env python3

import argparse
import json
import os
from pathlib import Path
from typing import Dict, Optional, Sequence

import requests
from pypdf import PdfReader

try:
    from scripts.manual_version_monitor import (
        BrowserFetcher,
        extract_pdf_text,
        make_text_diff,
        sha256_bytes,
        summarize_with_gemini,
    )
except ModuleNotFoundError:
    from manual_version_monitor import (
        BrowserFetcher,
        extract_pdf_text,
        make_text_diff,
        sha256_bytes,
        summarize_with_gemini,
    )


TARGETS = {
    "model3_cn": ("Model 3 大陆版", "model3-manual-current.pdf"),
    "model3_com": (
        "Model 3 国际版",
        "model3-international-manual-current.pdf",
    ),
    "modely_cn": ("Model Y 大陆版", "modely-manual-current.pdf"),
    "modely_com": (
        "Model Y 国际版",
        "modely-international-manual-current.pdf",
    ),
}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Compare each regional PDF baseline with its matching current PDF "
            "without changing RSS, state, or release assets."
        )
    )
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--baseline-dir", type=Path)
    parser.add_argument("--current-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--pair",
        nargs=2,
        type=Path,
        metavar=("OLD_PDF", "NEW_PDF"),
        help="Compare two explicit PDFs instead of using regional state.",
    )
    parser.add_argument(
        "--pair-name",
        default="Manual PDF pair",
        help="Name passed to Gemini and written to the pair report.",
    )
    parser.add_argument("--old-version", default="old PDF")
    parser.add_argument("--new-version", default="new PDF")
    parser.add_argument(
        "--output-name",
        default="manual-pair",
        help="Filename stem used by --pair outputs.",
    )
    parser.add_argument(
        "--target",
        action="append",
        choices=sorted(TARGETS),
        help="Limit the run to one or more target keys. Defaults to all targets.",
    )
    parser.add_argument(
        "--download-current",
        action="store_true",
        help="Download each current PDF URL with headless Chrome before comparing.",
    )
    parser.add_argument(
        "--gemini",
        action="store_true",
        help="Call Gemini using GEMINI_API_KEY after producing each text diff.",
    )
    parser.add_argument(
        "--gemini-model",
        default=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
    )
    parser.add_argument("--timeout", type=int, default=120)
    return parser.parse_args(argv)


def load_targets(path: Path) -> Dict[str, Dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 2:
        raise RuntimeError("The demo requires a schema_version 2 state file")
    targets = payload.get("targets")
    if not isinstance(targets, dict):
        raise RuntimeError("State file does not contain a targets object")
    return targets


def validate_target_state(
    key: str, value: Dict[str, object], expected_asset: str
) -> None:
    for field in ("version", "pdf_url", "pdf_sha256", "pdf_asset"):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise RuntimeError(f"{key}: state field {field!r} is missing or invalid")
    if value["pdf_asset"] != expected_asset:
        raise RuntimeError(
            f"{key}: expected regional asset {expected_asset!r}, "
            f"found {value['pdf_asset']!r}"
        )


def current_path(current_dir: Path, key: str) -> Path:
    return current_dir / f"{key}-current.pdf"


def pdf_metadata(path: Path) -> Dict[str, object]:
    reader = PdfReader(str(path))
    return {
        "encrypted": reader.is_encrypted,
        "pages": len(reader.pages),
    }


def compare_pair(args: argparse.Namespace) -> int:
    old_path, new_path = args.pair
    for label, path in (("Old", old_path), ("New", new_path)):
        if not path.is_file():
            raise RuntimeError(f"{label} PDF is missing: {path}")
        if not path.read_bytes().startswith(b"%PDF-"):
            raise RuntimeError(f"{label} file does not start with %PDF-: {path}")

    old_bytes = old_path.read_bytes()
    new_bytes = new_path.read_bytes()
    old_info = pdf_metadata(old_path)
    new_info = pdf_metadata(new_path)
    old_text = extract_pdf_text(old_path)
    new_text = extract_pdf_text(new_path)
    diff_text, additions, deletions = make_text_diff(old_text, new_text)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    diff_path = args.output_dir / f"{args.output_name}.diff.txt"
    diff_path.write_text(diff_text + "\n", encoding="utf-8")

    summary = None
    summary_path = None
    if args.gemini:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is required when --gemini is used")
        summary = summarize_with_gemini(
            requests.Session(),
            api_key,
            args.gemini_model,
            args.pair_name,
            args.old_version,
            args.new_version,
            diff_text,
            additions,
            deletions,
            args.timeout,
        )
        summary_path = args.output_dir / f"{args.output_name}.summary.txt"
        summary_path.write_text(summary + "\n", encoding="utf-8")

    result = {
        "pair_name": args.pair_name,
        "old_pdf": str(old_path),
        "new_pdf": str(new_path),
        "old_version": args.old_version,
        "new_version": args.new_version,
        "old_sha256": sha256_bytes(old_bytes),
        "new_sha256": sha256_bytes(new_bytes),
        "byte_identical": old_bytes == new_bytes,
        "old_encrypted": old_info["encrypted"],
        "new_encrypted": new_info["encrypted"],
        "old_pages": old_info["pages"],
        "new_pages": new_info["pages"],
        "old_text_characters": len(old_text),
        "new_text_characters": len(new_text),
        "additions": additions,
        "deletions": deletions,
        "diff_file": str(diff_path),
        "gemini_summary": summary,
        "gemini_summary_file": str(summary_path) if summary_path else None,
    }
    report_path = args.output_dir / f"{args.output_name}.report.json"
    report_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"Report: {report_path}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.timeout <= 0:
        raise ValueError("Timeout must be greater than zero")
    if args.pair:
        if args.download_current or args.target:
            raise RuntimeError(
                "--pair cannot be combined with --download-current or --target"
            )
        return compare_pair(args)

    missing_arguments = [
        name
        for name, value in (
            ("--state-file", args.state_file),
            ("--baseline-dir", args.baseline_dir),
            ("--current-dir", args.current_dir),
        )
        if value is None
    ]
    if missing_arguments:
        raise RuntimeError(
            "Regional mode requires: " + ", ".join(missing_arguments)
        )

    targets = load_targets(args.state_file)
    selected = args.target or list(TARGETS)
    missing = [key for key in selected if key not in targets]
    if missing:
        raise RuntimeError(f"Targets are missing from state: {', '.join(missing)}")

    args.current_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    browser = BrowserFetcher(args.timeout) if args.download_current else None
    session = requests.Session()
    report: Dict[str, object] = {"results": {}, "errors": []}

    try:
        for key in selected:
            target_state = targets[key]
            display_name, expected_asset = TARGETS[key]
            validate_target_state(key, target_state, expected_asset)
            baseline_path = args.baseline_dir / expected_asset
            live_path = current_path(args.current_dir, key)

            try:
                if not baseline_path.is_file():
                    raise RuntimeError(f"Baseline PDF is missing: {baseline_path}")
                if browser is not None:
                    live_path.write_bytes(browser.download(str(target_state["pdf_url"])))
                if not live_path.is_file():
                    raise RuntimeError(
                        f"Current PDF is missing: {live_path}; use --download-current"
                    )

                baseline_bytes = baseline_path.read_bytes()
                live_bytes = live_path.read_bytes()
                if not baseline_bytes.startswith(b"%PDF-"):
                    raise RuntimeError("Baseline file does not start with %PDF-")
                if not live_bytes.startswith(b"%PDF-"):
                    raise RuntimeError("Current file does not start with %PDF-")

                baseline_hash = sha256_bytes(baseline_bytes)
                live_hash = sha256_bytes(live_bytes)
                expected_hash = str(target_state["pdf_sha256"])
                if baseline_hash != expected_hash:
                    raise RuntimeError(
                        "Baseline SHA-256 does not match manual-version-state.json"
                    )

                baseline_info = pdf_metadata(baseline_path)
                live_info = pdf_metadata(live_path)
                old_text = extract_pdf_text(baseline_path)
                new_text = extract_pdf_text(live_path)
                diff_text, additions, deletions = make_text_diff(old_text, new_text)
                diff_path = args.output_dir / f"{key}.diff.txt"
                diff_path.write_text(diff_text + "\n", encoding="utf-8")

                summary = None
                summary_path = None
                if args.gemini:
                    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
                    if not api_key:
                        raise RuntimeError(
                            "GEMINI_API_KEY is required when --gemini is used"
                        )
                    version = str(target_state["version"])
                    summary = summarize_with_gemini(
                        session,
                        api_key,
                        args.gemini_model,
                        display_name,
                        version,
                        version,
                        diff_text,
                        additions,
                        deletions,
                        args.timeout,
                    )
                    summary_path = args.output_dir / f"{key}.summary.txt"
                    summary_path.write_text(summary + "\n", encoding="utf-8")

                result = {
                    "display_name": display_name,
                    "version": target_state["version"],
                    "baseline_asset": target_state["pdf_asset"],
                    "baseline_sha256": baseline_hash,
                    "current_sha256": live_hash,
                    "byte_identical": baseline_hash == live_hash,
                    "baseline_encrypted": baseline_info["encrypted"],
                    "current_encrypted": live_info["encrypted"],
                    "baseline_pages": baseline_info["pages"],
                    "current_pages": live_info["pages"],
                    "baseline_text_characters": len(old_text),
                    "current_text_characters": len(new_text),
                    "additions": additions,
                    "deletions": deletions,
                    "diff_file": str(diff_path),
                    "gemini_summary": summary,
                    "gemini_summary_file": str(summary_path) if summary_path else None,
                }
                report["results"][key] = result
                print(json.dumps({key: result}, ensure_ascii=False, indent=2))
            except Exception as error:
                message = f"{display_name}: {type(error).__name__}: {error}"
                report["errors"].append(message)
                print(message)
    finally:
        if browser is not None:
            browser.close()

    report_path = args.output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Report: {report_path}")
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
