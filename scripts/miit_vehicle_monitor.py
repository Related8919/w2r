#!/usr/bin/env python3

import argparse
import os
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def parse_feed_items(xml_content: bytes) -> List[Tuple[str, str]]:
    root = ET.fromstring(xml_content)
    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if title and link:
            items.append((title, link))
    return items


def new_article_urls(
    current_xml: bytes, previous_xml: bytes, title_keyword: str
) -> List[str]:
    current_items = parse_feed_items(current_xml)
    previous_urls = (
        {
            link
            for title, link in parse_feed_items(previous_xml)
            if title_keyword in title
        }
        if previous_xml
        else set()
    )
    return [
        link
        for title, link in current_items
        if title_keyword in title and link not in previous_urls
    ]


def previous_feed_from_git(previous_ref: str, feed_path: Path) -> bytes:
    result = subprocess.run(
        ["git", "show", f"{previous_ref}:{feed_path.as_posix()}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        print(f"Previous feed is unavailable at {previous_ref}; current entries are new.")
        return b""
    return result.stdout


def validate_miit_url(url: str, allowed_host: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != allowed_host:
        raise ValueError(f"URL is outside the allowed MIIT host: {url}")


def find_attachments(
    html: bytes, article_url: str, attachment_keyword: str, allowed_host: str
) -> List[Tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    attachments = []
    for anchor in soup.select("#con_con a[href]"):
        name = anchor.get_text(" ", strip=True)
        url = urljoin(article_url, anchor["href"])
        suffix = Path(urlparse(url).path).suffix.lower()
        if attachment_keyword not in name or suffix not in {".doc", ".docx"}:
            continue
        validate_miit_url(url, allowed_host)
        attachments.append((name, url))
    return attachments


def extract_docx_text(path: Path) -> str:
    text_parts = []
    with zipfile.ZipFile(path) as archive:
        xml_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("word/") and name.endswith(".xml")
        )
        for name in xml_names:
            root = ET.fromstring(archive.read(name))
            for element in root.iter():
                if element.tag.endswith("}t") and element.text:
                    text_parts.append(element.text)
    return "\n".join(text_parts)


def extract_doc_text(path: Path) -> str:
    result = subprocess.run(
        ["antiword", "-m", "UTF-8.txt", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"antiword failed for {path.name}: {message}")
    return result.stdout.decode("utf-8", errors="replace")


def extract_document_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return extract_docx_text(path)
    if suffix == ".doc":
        return extract_doc_text(path)
    raise ValueError(f"Unsupported document type: {path.name}")


def build_bark_url(
    base_url: str,
    token: str,
    title: str,
    body: str,
    article_url: str,
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
    return f"{path}?{urlencode({'url': article_url, 'group': group})}"


def send_bark(
    session: requests.Session,
    base_url: str,
    token: str,
    title: str,
    body: str,
    article_url: str,
    group: str,
    timeout: int,
) -> None:
    bark_url = build_bark_url(
        base_url, token, title, body, article_url, group
    )
    try:
        response = session.get(bark_url, timeout=timeout)
    except requests.RequestException as error:
        raise RuntimeError(
            f"Bark request failed: {type(error).__name__}"
        ) from None
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"Bark returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        return
    if payload.get("code") not in (None, 200):
        raise RuntimeError(f"Bark rejected the notification: {payload}")


def safe_filename(name: str, fallback_url: str, index: int) -> str:
    suffix = Path(urlparse(fallback_url).path).suffix.lower()
    cleaned = "".join(
        character if character not in '\\/:*?"<>|' else "_" for character in name
    ).strip()
    if not cleaned.lower().endswith(suffix):
        cleaned = f"{cleaned}{suffix}"
    return cleaned or f"attachment-{index}{suffix}"


def process_article(
    session: requests.Session,
    article_url: str,
    attachment_keyword: str,
    document_search_text: str,
    allowed_host: str,
    timeout: int,
    bark_base_url: str,
    bark_token: str,
    bark_title: str,
    bark_group: str,
    dry_run: bool,
) -> int:
    validate_miit_url(article_url, allowed_host)
    response = session.get(article_url, timeout=timeout)
    response.raise_for_status()
    attachments = find_attachments(
        response.content, article_url, attachment_keyword, allowed_host
    )
    if not attachments:
        print(f"No matching DOC attachment found: {article_url}")
        return 0

    matches = 0
    with tempfile.TemporaryDirectory(prefix="miit-vehicle-") as temp_dir:
        for index, (name, attachment_url) in enumerate(attachments, start=1):
            filename = safe_filename(name, attachment_url, index)
            path = Path(temp_dir) / filename
            print(f"Downloading attachment: {name}")
            download = session.get(
                attachment_url,
                headers={"Referer": article_url},
                timeout=timeout,
            )
            download.raise_for_status()
            path.write_bytes(download.content)

            document_text = extract_document_text(path)
            if document_search_text not in document_text:
                print(f"Search text not found in: {name}")
                continue

            matches += 1
            body = f"附件 {name} 中发现：{document_search_text}"
            if dry_run:
                print(f"DRY_RUN: Bark notification would be sent for {name}")
                continue
            if not bark_token:
                raise RuntimeError("BARK_TOKEN GitHub Secret is not configured")
            send_bark(
                session,
                bark_base_url,
                bark_token,
                bark_title,
                body,
                article_url,
                bark_group,
                timeout,
            )
            print(f"Bark notification sent for: {name}")
    return matches


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is empty: {name}")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check new MIIT vehicle notices and notify through Bark."
    )
    parser.add_argument("--feed", type=Path)
    parser.add_argument("--previous-ref", default="HEAD^")
    parser.add_argument("--article-url")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    title_keyword = required_env("ARTICLE_TITLE_KEYWORD")
    attachment_keyword = required_env("ATTACHMENT_NAME_KEYWORD")
    document_search_text = required_env("DOCUMENT_SEARCH_TEXT")
    allowed_host = required_env("ALLOWED_HOST")
    bark_base_url = required_env("BARK_BASE_URL")
    bark_title = required_env("BARK_TITLE")
    bark_group = required_env("BARK_GROUP")
    bark_token = os.environ.get("BARK_TOKEN", "").strip()
    timeout = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "30"))
    dry_run = env_bool("DRY_RUN")

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    if args.article_url:
        article_urls = [args.article_url]
    else:
        if args.feed is None:
            raise RuntimeError("--feed is required unless --article-url is provided")
        current_xml = args.feed.read_bytes()
        previous_xml = previous_feed_from_git(args.previous_ref, args.feed)
        article_urls = new_article_urls(
            current_xml, previous_xml, title_keyword
        )

    if not article_urls:
        print("No new matching MIIT vehicle notice was found.")
        return 0

    total_matches = 0
    for article_url in article_urls:
        print(f"Processing article: {article_url}")
        total_matches += process_article(
            session=session,
            article_url=article_url,
            attachment_keyword=attachment_keyword,
            document_search_text=document_search_text,
            allowed_host=allowed_host,
            timeout=timeout,
            bark_base_url=bark_base_url,
            bark_token=bark_token,
            bark_title=bark_title,
            bark_group=bark_group,
            dry_run=dry_run,
        )
    print(f"Completed: {len(article_urls)} article(s), {total_matches} match(es).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
