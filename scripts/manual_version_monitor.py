#!/usr/bin/env python3

import argparse
import difflib
import hashlib
import json
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urljoin

import requests
from bs4 import BeautifulSoup


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
STATE_SCHEMA_VERSION = 1
MAX_GEMINI_DIFF_CHARS = 120000


@dataclass(frozen=True)
class Target:
    key: str
    name: str
    page_url: str
    rss_path: Path
    pdf_asset: str


class BrowserFetcher:
    def __init__(self, timeout: int):
        self.timeout = timeout
        self.driver = None

    def _driver(self):
        if self.driver is None:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options

            options = Options()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--window-size=1920x1080")
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(self.timeout)
        return self.driver

    def page_html(self, url: str, ready_css: str) -> bytes:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as conditions

        driver = self._driver()
        driver.get(url)
        WebDriverWait(driver, self.timeout).until(
            conditions.presence_of_element_located((By.CSS_SELECTOR, ready_css))
        )
        return driver.page_source.encode("utf-8")

    def download(self, url: str) -> bytes:
        driver = self._driver()
        with tempfile.TemporaryDirectory(prefix="manual-browser-download-") as directory:
            driver.execute_cdp_cmd(
                "Browser.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": directory},
            )
            driver.execute_script(
                "const link = document.createElement('a');"
                "link.href = arguments[0]; link.download = 'manual.pdf';"
                "document.body.appendChild(link); link.click(); link.remove();",
                url,
            )
            deadline = time.monotonic() + self.timeout
            directory_path = Path(directory)
            while time.monotonic() < deadline:
                files = [
                    path
                    for path in directory_path.iterdir()
                    if path.is_file() and not path.name.endswith(".crdownload")
                ]
                if len(files) == 1:
                    return files[0].read_bytes()
                time.sleep(0.25)
        raise RuntimeError("Browser PDF download timed out")

    def close(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None


def normalize_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.replace(":", "：")).strip()
    return re.sub(r"：\s*", "：", normalized)


def parse_manual_page(
    html: bytes,
    page_url: str,
    version_container_css: str,
    version_text_prefix: str,
    pdf_link_css: str,
) -> Tuple[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    prefix = normalize_text(version_text_prefix)
    versions = [
        normalize_text(element.get_text(" ", strip=True))
        for element in soup.select(version_container_css)
    ]
    matches = [value for value in versions if value.startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one version paragraph starting with {prefix!r}; "
            f"found {len(matches)}"
        )

    pdf_links = soup.select(pdf_link_css)
    if len(pdf_links) != 1 or not pdf_links[0].get("href"):
        raise ValueError(
            f"Expected exactly one PDF link for selector {pdf_link_css!r}; "
            f"found {len(pdf_links)}"
        )
    return matches[0], urljoin(page_url, pdf_links[0]["href"])


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append(f"\n===== Page {number} =====\n{text.strip()}")
    return "\n".join(pages).strip()


def make_text_diff(old_text: str, new_text: str) -> Tuple[str, int, int]:
    if not old_text.strip() and not new_text.strip():
        return "PDF 无可提取的文本层，未执行 OCR。", 0, 0
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile="previous.pdf",
            tofile="current.pdf",
            lineterm="",
        )
    )
    additions = sum(
        1 for line in lines if line.startswith("+") and not line.startswith("+++")
    )
    deletions = sum(
        1 for line in lines if line.startswith("-") and not line.startswith("---")
    )
    return "\n".join(lines) or "PDF 可提取文字没有差异。", additions, deletions


def fallback_summary(old_version: str, new_version: str, additions: int, deletions: int) -> str:
    return (
        f"软件版本由 {old_version} 更新为 {new_version}。"
        f"PDF 文本差异统计：新增 {additions} 行，删除 {deletions} 行。"
    )


def build_bark_url(
    base_url: str,
    token: str,
    title: str,
    body: str,
    link_url: str,
    group: str,
) -> str:
    path = "/".join(
        [
            base_url.rstrip("/"),
            quote(token, safe=""),
            quote(title, safe=""),
            quote(body, safe=""),
        ]
    )
    return f"{path}?{urlencode({'url': link_url, 'group': group})}"


def send_bark(
    session: requests.Session,
    base_url: str,
    token: str,
    title: str,
    body: str,
    link_url: str,
    group: str,
    timeout: int,
) -> None:
    if not token:
        raise RuntimeError("BARK_TOKEN GitHub Secret is not configured")
    bark_url = build_bark_url(base_url, token, title, body, link_url, group)
    try:
        response = session.get(bark_url, timeout=timeout)
    except requests.RequestException as error:
        raise RuntimeError(f"Bark request failed: {type(error).__name__}") from None
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Bark returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return
    if payload.get("code") not in (None, 200):
        raise RuntimeError("Bark rejected the notification")


def bark_body(
    old_version: str,
    new_version: str,
    additions: int,
    deletions: int,
    summary: str,
) -> str:
    compact_summary = re.sub(r"\s+", " ", summary).strip()[:500]
    return (
        f"{old_version} -> {new_version}；"
        f"PDF 新增 {additions} 行，删除 {deletions} 行。{compact_summary}"
    )


def summarize_with_gemini(
    session: requests.Session,
    api_key: str,
    model: str,
    target_name: str,
    old_version: str,
    new_version: str,
    diff_text: str,
    additions: int,
    deletions: int,
    timeout: int,
) -> str:
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY GitHub Secret is not configured")
    clipped = diff_text[:MAX_GEMINI_DIFF_CHARS]
    truncation = "\n\n差异过长，以上内容已截断。" if len(diff_text) > len(clipped) else ""
    prompt = (
        f"请用简体中文总结 {target_name} 车主手册的更新，重点说明对车主有实际影响的变化。\n"
        f"旧软件版本：{old_version}\n新软件版本：{new_version}\n"
        f"文本差异统计：新增 {additions} 行，删除 {deletions} 行。\n"
        "不要猜测差异中没有的信息；使用简洁的项目符号。\n\n"
        f"PDF 文本差异：\n{clipped}{truncation}"
    )
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model, safe='')}:generateContent"
    )
    try:
        response = session.post(
            endpoint,
            headers={"x-goog-api-key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        parts = payload["candidates"][0]["content"]["parts"]
        summary = "\n".join(part.get("text", "") for part in parts).strip()
    except (requests.RequestException, ValueError, KeyError, IndexError) as error:
        raise RuntimeError(f"Gemini summary failed: {type(error).__name__}") from None
    if not summary:
        raise RuntimeError("Gemini summary failed: empty response")
    return summary


def load_state(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {"schema_version": STATE_SCHEMA_VERSION, "targets": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise RuntimeError("Unsupported manual state schema version")
    if not isinstance(state.get("targets"), dict):
        raise RuntimeError("Invalid manual state file")
    return state


def save_state(path: Path, state: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def rss_description(
    old_version: str,
    new_version: str,
    summary: str,
    additions: int,
    deletions: int,
) -> str:
    return (
        f"旧版本：{old_version}\n新版本：{new_version}\n"
        f"PDF 文本变化：新增 {additions} 行，删除 {deletions} 行\n\n{summary}"
    )


def ensure_rss(path: Path, target: Target) -> None:
    if path.exists():
        root = ET.parse(path).getroot()
        if root.find("channel") is None:
            raise RuntimeError(f"Invalid RSS file: {path}")
        return
    root = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(root, "channel")
    ET.SubElement(channel, "title").text = f"{target.name} 中文车主手册更新"
    ET.SubElement(channel, "link").text = target.page_url
    ET.SubElement(channel, "description").text = (
        f"{target.name} 中文车主手册软件版本和 PDF 内容变化"
    )
    ET.SubElement(channel, "language").text = "zh-cn"
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def update_rss(
    path: Path,
    target: Target,
    old_version: str,
    new_version: str,
    pdf_url: str,
    pdf_hash: str,
    summary: str,
    additions: int,
    deletions: int,
    detected_at: datetime,
    max_items: int,
) -> None:
    ensure_rss(path, target)
    root = ET.parse(path).getroot()
    channel = root.find("channel")

    guid_value = f"manual-{target.key}-{new_version}-{pdf_hash}"
    for item in channel.findall("item"):
        if item.findtext("guid") == guid_value:
            return

    item = ET.Element("item")
    ET.SubElement(item, "title").text = f"{target.name} 车主手册更新（{new_version}）"
    ET.SubElement(item, "link").text = pdf_url
    guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid.text = guid_value
    ET.SubElement(item, "pubDate").text = format_datetime(detected_at)
    ET.SubElement(item, "description").text = rss_description(
        old_version, new_version, summary, additions, deletions
    )

    first_item_index = next(
        (index for index, child in enumerate(channel) if child.tag == "item"),
        len(channel),
    )
    channel.insert(first_item_index, item)
    items = channel.findall("item")
    for old_item in items[max_items:]:
        channel.remove(old_item)

    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def fetch_manual_content(
    url: str,
    browser_fetcher: BrowserFetcher,
    ready_css: Optional[str] = None,
) -> bytes:
    if ready_css:
        return browser_fetcher.page_html(url, ready_css)
    return browser_fetcher.download(url)


def process_target(
    session: requests.Session,
    target: Target,
    state: Dict[str, object],
    state_dir: Path,
    version_container_css: str,
    version_text_prefix: str,
    pdf_link_css: str,
    gemini_api_key: str,
    gemini_model: str,
    timeout: int,
    max_items: int,
    browser_fetcher: BrowserFetcher,
    bark_base_url: str,
    bark_token: str,
    bark_title: str,
    bark_group: str,
) -> str:
    page_html = fetch_manual_content(
        target.page_url,
        browser_fetcher,
        version_container_css,
    )
    version, pdf_url = parse_manual_page(
        page_html,
        target.page_url,
        version_container_css,
        version_text_prefix,
        pdf_link_css,
    )
    targets_state = state["targets"]
    previous = targets_state.get(target.key)
    if previous and previous.get("version") == version:
        if not (state_dir / target.pdf_asset).exists():
            raise RuntimeError(f"Previous PDF asset is missing: {target.pdf_asset}")
        print(f"{target.name}: software version unchanged ({version})")
        return "unchanged"

    pdf_content = fetch_manual_content(pdf_url, browser_fetcher)
    if not pdf_content.startswith(b"%PDF-"):
        raise RuntimeError("Downloaded owner manual is not a valid PDF")
    pdf_hash = sha256_bytes(pdf_content)
    detected_at = datetime.now(timezone.utc)
    asset_path = state_dir / target.pdf_asset

    if previous is None:
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(pdf_content)
        targets_state[target.key] = {
            "version": version,
            "pdf_url": pdf_url,
            "pdf_sha256": pdf_hash,
            "pdf_asset": target.pdf_asset,
            "updated_at": detected_at.isoformat(),
        }
        ensure_rss(target.rss_path, target)
        print(f"{target.name}: baseline created ({version})")
        return "baseline"

    if not asset_path.exists():
        raise RuntimeError(f"Previous PDF asset is missing: {target.pdf_asset}")

    with tempfile.TemporaryDirectory(prefix=f"manual-{target.key}-") as temp_dir:
        new_path = Path(temp_dir) / "current.pdf"
        new_path.write_bytes(pdf_content)
        old_text = extract_pdf_text(asset_path)
        new_text = extract_pdf_text(new_path)
        diff_text, additions, deletions = make_text_diff(old_text, new_text)

    old_version = previous["version"]
    try:
        summary = summarize_with_gemini(
            session,
            gemini_api_key,
            gemini_model,
            target.name,
            old_version,
            version,
            diff_text,
            additions,
            deletions,
            timeout,
        )
    except RuntimeError as error:
        print(f"::warning::{target.name}: {error}")
        summary = fallback_summary(old_version, version, additions, deletions)

    update_rss(
        target.rss_path,
        target,
        old_version,
        version,
        pdf_url,
        pdf_hash,
        summary,
        additions,
        deletions,
        detected_at,
        max_items,
    )
    send_bark(
        session,
        bark_base_url,
        bark_token,
        f"{bark_title} - {target.name}",
        bark_body(old_version, version, additions, deletions, summary),
        pdf_url,
        bark_group,
        timeout,
    )
    print(f"{target.name}: Bark notification sent")
    asset_path.write_bytes(pdf_content)
    targets_state[target.key] = {
        "version": version,
        "pdf_url": pdf_url,
        "pdf_sha256": pdf_hash,
        "pdf_asset": target.pdf_asset,
        "updated_at": detected_at.isoformat(),
    }
    print(f"{target.name}: updated {old_version} -> {version}")
    return "updated"


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is empty: {name}")
    return value


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor Chinese owner manual versions.")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    state_path = args.state_dir / "manual-version-state.json"
    state = load_state(state_path)
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
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    browser_fetcher = BrowserFetcher(
        int(os.environ.get("MANUAL_REQUEST_TIMEOUT_SECONDS", "30"))
    )
    errors: List[str] = []
    results = {}
    for target in targets:
        try:
            results[target.key] = process_target(
                session,
                target,
                state,
                args.state_dir,
                required_env("MANUAL_VERSION_CONTAINER_CSS"),
                required_env("MANUAL_VERSION_TEXT_PREFIX"),
                required_env("MANUAL_PDF_LINK_CSS"),
                os.environ.get("GEMINI_API_KEY", "").strip(),
                required_env("GEMINI_MODEL"),
                int(os.environ.get("MANUAL_REQUEST_TIMEOUT_SECONDS", "30")),
                int(os.environ.get("MANUAL_RSS_MAX_ITEMS", "50")),
                browser_fetcher,
                required_env("BARK_BASE_URL"),
                os.environ.get("BARK_TOKEN", "").strip(),
                required_env("MANUAL_BARK_TITLE"),
                required_env("MANUAL_BARK_GROUP"),
            )
        except Exception as error:
            message = f"{target.name}: {type(error).__name__}: {error}"
            print(f"::error::{message}")
            errors.append(message)
            results[target.key] = "failed"

    try:
        browser_fetcher.close()
    except Exception as error:
        print(f"::warning::Headless Chrome cleanup failed: {type(error).__name__}")
    save_state(state_path, state)
    args.result_file.parent.mkdir(parents=True, exist_ok=True)
    args.result_file.write_text(
        json.dumps({"results": results, "errors": errors}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
