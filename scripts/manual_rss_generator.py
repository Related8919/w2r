#!/usr/bin/env python3

import argparse
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence

try:
    from scripts.manual_version_monitor import (
        BrowserFetcher,
        Target,
        configured_targets,
        normalize_text,
        parse_manual_page,
        pending_rss_entry,
        required_env,
    )
except ModuleNotFoundError:
    from manual_version_monitor import (
        BrowserFetcher,
        Target,
        configured_targets,
        normalize_text,
        parse_manual_page,
        pending_rss_entry,
        required_env,
    )


def ensure_feed(path: Path, target: Target) -> ET.ElementTree:
    if path.exists():
        tree = ET.parse(path)
        if tree.getroot().find("channel") is None:
            raise RuntimeError(f"Invalid RSS file: {path}")
        return tree

    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = f"{target.name} 中文车主手册更新"
    ET.SubElement(channel, "link").text = target.feed_url or target.page_url
    ET.SubElement(channel, "description").text = (
        f"{target.name} 中文车主手册软件版本和 PDF 内容变化"
    )
    ET.SubElement(channel, "language").text = "zh-cn"
    return ET.ElementTree(root)


def write_feed(path: Path, tree: ET.ElementTree) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree.getroot(), space="  ")
    tree.write(path, encoding="utf-8", xml_declaration=True)


def add_version_entry(
    target: Target,
    version: str,
    pdf_url: str,
    detected_at: datetime,
    max_items: int,
) -> bool:
    tree = ensure_feed(target.rss_path, target)
    channel = tree.getroot().find("channel")
    guid_value = target.guid_for(version)
    accepted_guids = {guid_value}
    if target.legacy_guid_prefix:
        accepted_guids.add(f"{target.legacy_guid_prefix}{version}")
    for item in channel.findall("item"):
        if (item.findtext("guid") or "").strip() in accepted_guids:
            return False

    item = ET.Element("item")
    ET.SubElement(item, "title").text = target.item_title(version)
    ET.SubElement(item, "link").text = pdf_url
    guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid.text = guid_value
    ET.SubElement(item, "pubDate").text = format_datetime(detected_at)
    ET.SubElement(item, "description").text = ""

    first_item_index = next(
        (index for index, child in enumerate(channel) if child.tag == "item"),
        len(channel),
    )
    channel.insert(first_item_index, item)
    for old_item in channel.findall("item")[max_items:]:
        channel.remove(old_item)
    write_feed(target.rss_path, tree)
    return True


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate pending RSS entries for owner manual versions."
    )
    parser.add_argument("--result-file", type=Path)
    return parser.parse_args(argv)


def write_result(
    path: Optional[Path],
    results: Dict[str, str],
    pending_targets: List[str],
    errors: List[str],
) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "results": results,
                "pending_targets": pending_targets,
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    timeout = int(os.environ.get("MANUAL_REQUEST_TIMEOUT_SECONDS", "120"))
    max_items = int(os.environ.get("MANUAL_RSS_MAX_ITEMS", "50"))
    errors: List[str] = []
    results: Dict[str, str] = {}
    try:
        version_css = required_env("MANUAL_VERSION_CONTAINER_CSS")
        version_prefix = required_env("MANUAL_VERSION_TEXT_PREFIX")
        pdf_css = required_env("MANUAL_PDF_LINK_CSS")
        targets = configured_targets()
    except Exception as error:
        message = f"Configuration: {type(error).__name__}: {error}"
        print(f"::error::{message}")
        write_result(args.result_file, results, [], [message])
        return 1

    browser = BrowserFetcher(timeout)
    try:
        for target in targets:
            try:
                html = browser.page_html(target.page_url, version_prefix)
                version, pdf_url = parse_manual_page(
                    html,
                    target.page_url,
                    version_css,
                    version_prefix,
                    pdf_css,
                )
                version = normalize_text(version)
                if add_version_entry(
                    target,
                    version,
                    pdf_url,
                    datetime.now(timezone.utc),
                    max_items,
                ):
                    results[target.key] = "added"
                    print(
                        f"{target.display_name}: added RSS placeholder for {version}"
                    )
                else:
                    results[target.key] = "unchanged"
                    print(f"{target.display_name}: RSS already contains {version}")
            except Exception as error:
                message = (
                    f"{target.display_name}: {type(error).__name__}: {error}"
                )
                print(f"::error::{message}")
                errors.append(message)
                results[target.key] = "failed"
    finally:
        try:
            browser.close()
        except Exception as error:
            print(f"::warning::Headless Chrome cleanup failed: {type(error).__name__}")

    pending_targets: List[str] = []
    for target in targets:
        try:
            if pending_rss_entry(target.rss_path, target) is not None:
                pending_targets.append(target.key)
        except Exception as error:
            message = (
                f"{target.display_name} pending RSS check: "
                f"{type(error).__name__}: {error}"
            )
            print(f"::error::{message}")
            errors.append(message)
            results[target.key] = "failed"
    write_result(args.result_file, results, pending_targets, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
