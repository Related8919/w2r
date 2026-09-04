#!/usr/bin/env python3

import argparse
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

try:
    from scripts.manual_version_monitor import (
        BrowserFetcher,
        Target,
        configured_targets,
        extracted_version_matches,
        extract_pdf_cover_version,
        load_state,
        manual_pdf_url,
        normalize_text,
        required_env,
        sha256_bytes,
        urls_equivalent,
    )
except ModuleNotFoundError:
    from manual_version_monitor import (
        BrowserFetcher,
        Target,
        configured_targets,
        extracted_version_matches,
        extract_pdf_cover_version,
        load_state,
        manual_pdf_url,
        normalize_text,
        required_env,
        sha256_bytes,
        urls_equivalent,
    )


CANDIDATE_SCHEMA_VERSION = 1
CANDIDATE_MANIFEST_NAME = "manual-pdf-candidates.json"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True)
class FeedEntry:
    title: str
    pdf_url: str
    guid: str
    description: str


class DirectHtmlPageFetcher:
    """Read the live mainland page without a browser or search index."""

    def __init__(
        self,
        timeout: int,
        version_container_css: str,
        version_text_prefix: str,
        pdf_link_css: str,
        session: Optional[requests.Session] = None,
    ):
        if timeout <= 0:
            raise ValueError("Manual request timeout must be greater than zero")
        self.timeout = timeout
        self.version_container_css = version_container_css
        self.version_text_prefix = version_text_prefix
        self.pdf_link_css = pdf_link_css
        self.session = session or create_http_session()
        self._owns_session = session is None

    def fetch(self, page_url: str) -> Tuple[str, str]:
        response = self.session.get(page_url, timeout=self.timeout)
        response.raise_for_status()
        return parse_html_manual_page(
            response.content,
            page_url,
            self.version_container_css,
            self.version_text_prefix,
            self.pdf_link_css,
        )

    def download_pdf(self, pdf_url: str) -> bytes:
        response = self.session.get(pdf_url, timeout=self.timeout)
        response.raise_for_status()
        return response.content

    def close(self) -> None:
        if self._owns_session:
            self.session.close()


def create_http_session() -> requests.Session:
    session = requests.Session()
    # Do not let runner proxy variables silently select a cached representation.
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    return session


def _origin(url: str) -> Tuple[str, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Manual URL is not an absolute HTTP URL: {url}")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"Manual URL has an invalid port: {url}") from error
    return (
        parsed.scheme.lower(),
        parsed.hostname.lower(),
        port or (443 if parsed.scheme.lower() == "https" else 80),
    )


def validate_page_pdf_url(page_url: str, pdf_url: str) -> None:
    if _origin(page_url) != _origin(pdf_url):
        raise ValueError("Manual PDF link must use the same origin as the page URL")
    parsed = urlsplit(pdf_url)
    if parsed.fragment:
        raise ValueError("Manual PDF link must not contain a fragment")
    if not parsed.path.endswith("/Owners_Manual.pdf"):
        raise ValueError("Manual PDF link must end with /Owners_Manual.pdf")


def parse_html_manual_page(
    content: bytes,
    page_url: str,
    version_container_css: str,
    version_text_prefix: str,
    pdf_link_css: str,
) -> Tuple[str, str]:
    soup = BeautifulSoup(content, "html.parser")
    prefix = re.sub(r"\s+：", "：", normalize_text(version_text_prefix))
    version_matches = [
        text
        for node in soup.select(version_container_css)
        if (
            text := re.sub(
                r"\s+：", "：", normalize_text(node.get_text(" ", strip=True))
            )
        ).startswith(prefix)
    ]
    if len(version_matches) != 1:
        raise ValueError(
            f"Expected exactly one HTML paragraph starting with {prefix!r}; "
            f"found {len(version_matches)}"
        )

    pdf_nodes = soup.select(pdf_link_css)
    if len(pdf_nodes) != 1:
        raise ValueError(f"Expected exactly one HTML PDF link; found {len(pdf_nodes)}")
    href = (pdf_nodes[0].get("href") or "").strip()
    if not href:
        raise ValueError("The matched HTML PDF link has no href")
    pdf_url = urljoin(page_url, href)
    validate_page_pdf_url(page_url, pdf_url)
    return version_matches[0], pdf_url


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


def _accepted_guids(target: Target, version: str) -> List[str]:
    guids = [target.guid_for(version)]
    if target.legacy_guid_prefix:
        guids.append(f"{target.legacy_guid_prefix}{version}")
    return guids


def find_version_entry(target: Target, version: str) -> Optional[FeedEntry]:
    if not target.rss_path.exists():
        return None
    root = ET.parse(target.rss_path).getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError(f"Invalid RSS file: {target.rss_path}")
    accepted_guids = set(_accepted_guids(target, version))
    matches: List[FeedEntry] = []
    for item in channel.findall("item"):
        guid = (item.findtext("guid") or "").strip()
        if guid not in accepted_guids:
            continue
        title = (item.findtext("title") or "").strip()
        pdf_url = (item.findtext("link") or "").strip()
        if not title or not pdf_url:
            raise RuntimeError(f"Incomplete manual RSS item: {target.rss_path}")
        if guid.startswith(target.guid_prefix):
            pass
        elif target.legacy_guid_prefix and guid == (
            f"{target.legacy_guid_prefix}{version}"
        ):
            if title != version:
                continue
        matches.append(
            FeedEntry(
                title=title,
                pdf_url=pdf_url,
                guid=guid,
                description=(item.findtext("description") or "").strip(),
            )
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"RSS contains multiple entries for {target.display_name} {version}"
        )
    return matches[0] if matches else None


def _item_belongs_to_target(item: ET.Element, target: Target) -> bool:
    guid = (item.findtext("guid") or "").strip()
    if guid.startswith(target.guid_prefix):
        return True
    if target.legacy_guid_prefix and guid.startswith(target.legacy_guid_prefix):
        version = guid[len(target.legacy_guid_prefix) :].strip()
        return (item.findtext("title") or "").strip() == version
    return False


def _item_version(item: ET.Element, target: Target) -> str:
    guid = (item.findtext("guid") or "").strip()
    prefix = (
        target.guid_prefix
        if guid.startswith(target.guid_prefix)
        else target.legacy_guid_prefix or ""
    )
    return guid[len(prefix) :].strip()


def stale_pending_versions(target: Target, current_version: str) -> List[str]:
    if not target.rss_path.exists():
        return []
    root = ET.parse(target.rss_path).getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError(f"Invalid RSS file: {target.rss_path}")
    return [
        _item_version(item, target)
        for item in channel.findall("item")
        if _item_belongs_to_target(item, target)
        and not (item.findtext("description") or "").strip()
        and _item_version(item, target) != current_version
    ]


def remove_stale_pending_entries(target: Target, current_version: str) -> List[str]:
    if not target.rss_path.exists():
        return []
    tree = ET.parse(target.rss_path)
    channel = tree.getroot().find("channel")
    if channel is None:
        raise RuntimeError(f"Invalid RSS file: {target.rss_path}")
    removed: List[str] = []
    for item in list(channel.findall("item")):
        if not _item_belongs_to_target(item, target):
            continue
        if (item.findtext("description") or "").strip():
            continue
        version = _item_version(item, target)
        if version == current_version:
            continue
        removed.append(version)
        channel.remove(item)
    if removed:
        write_feed(target.rss_path, tree)
    return removed


def target_has_pending_entry(target: Target) -> bool:
    if not target.rss_path.exists():
        return False
    root = ET.parse(target.rss_path).getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError(f"Invalid RSS file: {target.rss_path}")
    return any(
        _item_belongs_to_target(item, target)
        and not (item.findtext("description") or "").strip()
        for item in channel.findall("item")
    )


def add_version_entry(
    target: Target,
    version: str,
    pdf_url: str,
    detected_at: datetime,
    max_items: int,
) -> bool:
    tree = ensure_feed(target.rss_path, target)
    channel = tree.getroot().find("channel")
    accepted_guids = set(_accepted_guids(target, version))
    for item in channel.findall("item"):
        if (item.findtext("guid") or "").strip() in accepted_guids:
            return False

    item = ET.Element("item")
    ET.SubElement(item, "title").text = target.item_title(version)
    ET.SubElement(item, "link").text = pdf_url
    guid = ET.SubElement(item, "guid", {"isPermaLink": "false"})
    guid.text = target.guid_for(version)
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


def state_matches_version(
    targets_state: Mapping[str, object], target: Target, version: str
) -> bool:
    previous = targets_state.get(target.key)
    if not isinstance(previous, Mapping):
        return False
    pdf_url = previous.get("pdf_url")
    pdf_hash = previous.get("pdf_sha256")
    if not (
        previous.get("version") == version
        and previous.get("pdf_asset") == target.pdf_asset
        and isinstance(pdf_url, str)
        and isinstance(pdf_hash, str)
        and re.fullmatch(r"[0-9a-f]{64}", pdf_hash)
    ):
        return False
    try:
        validate_page_pdf_url(target.page_url, pdf_url)
    except (TypeError, ValueError):
        return False
    parsed = urlsplit(pdf_url)
    if not parsed.hostname or not parsed.hostname.lower().endswith(
        f".{target.region_code}"
    ):
        return False
    if target.region_code == "com":
        return urls_equivalent(pdf_url, manual_pdf_url(target.page_url))
    return target.region_code == "cn"


def validate_candidate_asset(target: Target) -> None:
    if (
        not target.pdf_asset
        or Path(target.pdf_asset).name != target.pdf_asset
        or target.pdf_asset in {".", ".."}
    ):
        raise ValueError(
            f"Unsafe PDF candidate asset name for {target.display_name}: "
            f"{target.pdf_asset!r}"
        )


def extract_downloaded_pdf_version(
    candidate_dir: Path,
    target: Target,
    content: bytes,
    version_text_prefix: str,
) -> str:
    if not content.startswith(b"%PDF-"):
        raise ValueError("Downloaded owner manual is not a valid PDF")
    validate_candidate_asset(target)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    temporary = candidate_dir / f".{target.pdf_asset}.verify.tmp"
    try:
        temporary.write_bytes(content)
        return extract_pdf_cover_version(temporary, version_text_prefix)
    finally:
        temporary.unlink(missing_ok=True)


def save_candidate(candidate_dir: Path, target: Target, content: bytes) -> None:
    validate_candidate_asset(target)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    path = candidate_dir / target.pdf_asset
    temporary = candidate_dir / f".{target.pdf_asset}.tmp"
    temporary.write_bytes(content)
    temporary.replace(path)


def source_run_metadata() -> Dict[str, object]:
    values = {
        "id": required_env("GITHUB_RUN_ID"),
        "number": required_env("GITHUB_RUN_NUMBER"),
        "attempt": required_env("GITHUB_RUN_ATTEMPT"),
    }
    result: Dict[str, object] = {}
    for key, value in values.items():
        try:
            number = int(value)
        except ValueError:
            raise ValueError(f"GITHUB_RUN_{key.upper()} must be a positive integer")
        if number <= 0 or str(number) != value:
            raise ValueError(f"GITHUB_RUN_{key.upper()} must be a positive integer")
        result[key] = number
    commit = os.environ.get("GITHUB_SHA", "").strip()
    if commit:
        result["commit"] = commit
    return result


def write_candidate_manifest(
    candidate_dir: Path,
    candidates: List[Dict[str, object]],
    source_run: Mapping[str, object],
) -> Path:
    candidate_dir.mkdir(parents=True, exist_ok=True)
    path = candidate_dir / CANDIDATE_MANIFEST_NAME
    temporary = candidate_dir / f".{CANDIDATE_MANIFEST_NAME}.tmp"
    temporary.write_text(
        json.dumps(
            {
                "schema_version": CANDIDATE_SCHEMA_VERSION,
                "source_run": dict(source_run),
                "candidates": candidates,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def clear_known_candidate_files(candidate_dir: Path, targets: Sequence[Target]) -> None:
    candidate_dir.mkdir(parents=True, exist_ok=True)
    names = {CANDIDATE_MANIFEST_NAME, f".{CANDIDATE_MANIFEST_NAME}.tmp"}
    for target in targets:
        validate_candidate_asset(target)
        names.update(
            {
                target.pdf_asset,
                f".{target.pdf_asset}.tmp",
                f".{target.pdf_asset}.verify.tmp",
            }
        )
    for name in names:
        path = candidate_dir / name
        if path.is_file():
            path.unlink()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate owner manual RSS entries and verified PDF candidates."
    )
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument(
        "--state-file",
        type=Path,
        help="Previously released state JSON; a missing file is treated as empty state.",
    )
    return parser.parse_args(argv)


def write_result(
    path: Optional[Path],
    results: Dict[str, str],
    pending_targets: List[str],
    candidates: List[Dict[str, object]],
    errors: List[str],
) -> None:
    if path is None:
        return
    candidate_targets = [str(candidate["target_key"]) for candidate in candidates]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "results": results,
                "pending_targets": pending_targets,
                "candidate_targets": candidate_targets,
                "candidate_count": len(candidates),
                "dispatch_required": bool(candidates),
                "errors": errors,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _candidate_record(
    target: Target,
    version: str,
    pdf_url: str,
    content: bytes,
    version_source: str,
    rss_guid: str,
    mode: str,
) -> Dict[str, object]:
    return {
        "target_key": target.key,
        "version": version,
        "pdf_url": pdf_url,
        "pdf_sha256": sha256_bytes(content),
        "pdf_size": len(content),
        "pdf_asset": target.pdf_asset,
        "candidate_file": target.pdf_asset,
        "version_source": version_source,
        "rss_guid": rss_guid,
        "mode": mode,
    }


def _process_verified_version(
    target: Target,
    version: str,
    pdf_url: str,
    pdf_content: bytes,
    version_source: str,
    targets_state: Mapping[str, object],
    candidate_dir: Path,
    max_items: int,
) -> Tuple[str, Optional[Dict[str, object]]]:
    removed = remove_stale_pending_entries(target, version)
    if removed:
        print(
            f"::warning::{target.display_name}: removed stale pending RSS versions: "
            + ", ".join(removed)
        )

    entry = find_version_entry(target, version)
    added = entry is None
    state_aligned = state_matches_version(targets_state, target, version)
    if not added and entry.description and state_aligned:
        return "unchanged", None

    if added:
        if not add_version_entry(
            target,
            version,
            pdf_url,
            datetime.now(timezone.utc),
            max_items,
        ):
            raise RuntimeError("Manual RSS entry appeared while it was being added")
        entry = find_version_entry(target, version)
        if entry is None:
            raise RuntimeError("New manual RSS entry could not be read back")

    mode = "pending" if not entry.description else "reconcile"
    save_candidate(candidate_dir, target, pdf_content)
    candidate = _candidate_record(
        target,
        version,
        pdf_url,
        pdf_content,
        version_source,
        entry.guid,
        mode,
    )
    if added:
        return "added", candidate
    if mode == "pending":
        return "pending", candidate
    return "reconcile", candidate


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    errors: List[str] = []
    results: Dict[str, str] = {}
    candidates: List[Dict[str, object]] = []
    targets: List[Target] = []
    source_run: Mapping[str, object] = {}
    html_fetcher: Optional[DirectHtmlPageFetcher] = None
    pdf_fetcher: Optional[BrowserFetcher] = None

    try:
        timeout = int(os.environ.get("MANUAL_REQUEST_TIMEOUT_SECONDS", "120"))
        max_items = int(os.environ.get("MANUAL_RSS_MAX_ITEMS", "50"))
        if timeout <= 0:
            raise ValueError("MANUAL_REQUEST_TIMEOUT_SECONDS must be positive")
        if max_items <= 0:
            raise ValueError("MANUAL_RSS_MAX_ITEMS must be positive")
        version_prefix = required_env("MANUAL_VERSION_TEXT_PREFIX")
        version_container_css = required_env("MANUAL_VERSION_CONTAINER_CSS")
        pdf_link_css = required_env("MANUAL_PDF_LINK_CSS")
        targets = configured_targets()
        source_run = source_run_metadata()
        clear_known_candidate_files(args.candidate_dir, targets)
        state = (
            load_state(args.state_file)
            if args.state_file is not None and args.state_file.exists()
            else {"schema_version": 2, "targets": {}}
        )
        targets_state = state["targets"]
        html_fetcher = DirectHtmlPageFetcher(
            timeout,
            version_container_css,
            version_prefix,
            pdf_link_css,
        )
        pdf_fetcher = BrowserFetcher(timeout)
    except Exception as error:
        message = f"Configuration: {type(error).__name__}: {error}"
        print(f"::error::{message}")
        errors.append(message)
        if source_run:
            write_candidate_manifest(args.candidate_dir, candidates, source_run)
        write_result(args.result_file, results, [], candidates, errors)
        return 1

    try:
        for target in targets:
            try:
                validate_candidate_asset(target)
                if target.region_code == "cn":
                    html_version, pdf_url = html_fetcher.fetch(target.page_url)
                    html_version = normalize_text(html_version)
                    print(
                        f"{target.display_name}: direct HTML reports "
                        f"{html_version} ({target.page_url})"
                    )
                    entry = find_version_entry(target, html_version)
                    state_aligned = state_matches_version(
                        targets_state, target, html_version
                    )
                    stale_pending = stale_pending_versions(target, html_version)
                    if (
                        entry is not None
                        and entry.description
                        and state_aligned
                        and not stale_pending
                    ):
                        results[target.key] = "unchanged"
                        print(
                            f"{target.display_name}: RSS and Release state already "
                            f"contain {html_version}; PDF download skipped"
                        )
                        continue
                    pdf_content = html_fetcher.download_pdf(pdf_url)
                    pdf_version = extract_downloaded_pdf_version(
                        args.candidate_dir,
                        target,
                        pdf_content,
                        version_prefix,
                    )
                    if html_version != pdf_version:
                        raise RuntimeError(
                            f"HTML version {html_version!r} does not match PDF "
                            f"cover version {pdf_version!r}"
                        )
                    version = html_version
                    version_source = "html+pdf"
                elif target.region_code == "com":
                    pdf_url = manual_pdf_url(target.page_url)
                    validate_page_pdf_url(target.page_url, pdf_url)
                    pdf_content = pdf_fetcher.download(pdf_url)
                    version = extract_downloaded_pdf_version(
                        args.candidate_dir,
                        target,
                        pdf_content,
                        version_prefix,
                    )
                    version_source = "pdf"
                    print(
                        f"{target.display_name}: PDF cover reports {version} "
                        f"({pdf_url})"
                    )
                else:
                    raise RuntimeError(
                        f"Unsupported manual region code: {target.region_code!r}"
                    )

                result, candidate = _process_verified_version(
                    target,
                    version,
                    pdf_url,
                    pdf_content,
                    version_source,
                    targets_state,
                    args.candidate_dir,
                    max_items,
                )
                results[target.key] = result
                if candidate is not None:
                    candidates.append(candidate)
                    print(
                        f"{target.display_name}: prepared {candidate['mode']} "
                        f"PDF candidate for {version}"
                    )
                else:
                    print(
                        f"{target.display_name}: RSS and Release state already "
                        f"contain {version}"
                    )
            except Exception as error:
                message = f"{target.display_name}: {type(error).__name__}: {error}"
                print(f"::error::{message}")
                errors.append(message)
                results[target.key] = "failed"
    finally:
        if html_fetcher is not None:
            try:
                html_fetcher.close()
            except Exception as error:
                print(
                    f"::warning::Direct HTML cleanup failed: {type(error).__name__}"
                )
        if pdf_fetcher is not None:
            try:
                pdf_fetcher.close()
            except Exception as error:
                print(
                    f"::warning::Headless Chrome cleanup failed: {type(error).__name__}"
                )

    pending_targets: List[str] = []
    for target in targets:
        try:
            if target_has_pending_entry(target):
                pending_targets.append(target.key)
        except Exception as error:
            message = (
                f"{target.display_name} pending RSS check: "
                f"{type(error).__name__}: {error}"
            )
            print(f"::error::{message}")
            errors.append(message)
            results[target.key] = "failed"

    write_candidate_manifest(args.candidate_dir, candidates, source_run)
    write_result(args.result_file, results, pending_targets, candidates, errors)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
