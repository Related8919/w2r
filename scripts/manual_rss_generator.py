#!/usr/bin/env python3

import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import List

try:
    from scripts.manual_version_monitor import (
        BrowserFetcher,
        Target,
        normalize_text,
        parse_manual_page,
        required_env,
    )
except ModuleNotFoundError:
    from manual_version_monitor import (
        BrowserFetcher,
        Target,
        normalize_text,
        parse_manual_page,
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
    ET.SubElement(channel, "link").text = target.page_url
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
    guid_value = f"manual-{target.key}-{version}"
    for item in channel.findall("item"):
        if item.findtext("guid") == guid_value:
            return False

    item = ET.Element("item")
    ET.SubElement(item, "title").text = version
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


def main() -> int:
    timeout = int(os.environ.get("MANUAL_REQUEST_TIMEOUT_SECONDS", "120"))
    max_items = int(os.environ.get("MANUAL_RSS_MAX_ITEMS", "50"))
    version_css = required_env("MANUAL_VERSION_CONTAINER_CSS")
    version_prefix = required_env("MANUAL_VERSION_TEXT_PREFIX")
    pdf_css = required_env("MANUAL_PDF_LINK_CSS")
    targets = [
        Target(
            "model3",
            "Model 3",
            required_env("MODEL3_MANUAL_URL"),
            Path(required_env("MODEL3_MANUAL_RSS_PATH")),
            "model3-manual-current.pdf",
        ),
        Target(
            "modely",
            "Model Y",
            required_env("MODELY_MANUAL_URL"),
            Path(required_env("MODELY_MANUAL_RSS_PATH")),
            "modely-manual-current.pdf",
        ),
    ]
    browser = BrowserFetcher(timeout)
    errors: List[str] = []
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
                    print(f"{target.name}: added RSS placeholder for {version}")
                else:
                    print(f"{target.name}: RSS already contains {version}")
            except Exception as error:
                message = f"{target.name}: {type(error).__name__}: {error}"
                print(f"::error::{message}")
                errors.append(message)
    finally:
        try:
            browser.close()
        except Exception as error:
            print(f"::warning::Headless Chrome cleanup failed: {type(error).__name__}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
