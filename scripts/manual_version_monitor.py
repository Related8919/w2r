#!/usr/bin/env python3

import argparse
import difflib
import hashlib
import html
import json
import os
import platform
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

import requests


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
STATE_SCHEMA_VERSION = 2
CANDIDATE_SCHEMA_VERSION = 1
CANDIDATE_MANIFEST_NAME = "manual-pdf-candidates.json"
MAX_GEMINI_DIFF_CHARS = 120000
MANUAL_PDF_FILENAME = "Owners_Manual.pdf"


@dataclass(frozen=True)
class Target:
    key: str
    name: str
    page_url: str
    rss_path: Path
    pdf_asset: str
    region_code: str = "cn"
    region_name: str = "大陆版"
    legacy_key: Optional[str] = None
    feed_url: Optional[str] = None

    @property
    def display_name(self) -> str:
        return f"{self.name} {self.region_name}"

    @property
    def guid_prefix(self) -> str:
        model_key = self.legacy_key
        if model_key is None and self.key.endswith(("_cn", "_com")):
            model_key = self.key.rsplit("_", 1)[0]
        return f"manual-{model_key or self.key}-{self.region_code}-"

    @property
    def legacy_guid_prefix(self) -> Optional[str]:
        if self.legacy_key is None:
            return None
        return f"manual-{self.legacy_key}-"

    def guid_for(self, version: str) -> str:
        return f"{self.guid_prefix}{version}"

    def item_title(self, version: str) -> str:
        return f"{self.region_name}｜{version}"


@dataclass(frozen=True)
class PdfCandidate:
    target_key: str
    version: str
    pdf_url: str
    pdf_sha256: str
    pdf_size: int
    pdf_asset: str
    candidate_file: str
    version_source: str
    rss_guid: str
    mode: str
    path: Path


@dataclass(frozen=True)
class RssEntry:
    title: str
    pdf_url: str
    guid: str
    description: str


@dataclass(frozen=True)
class SourceRun:
    run_id: int
    number: int
    attempt: int
    commit: Optional[str] = None

    @property
    def order(self) -> Tuple[int, int]:
        return self.number, self.attempt

    def as_state(self) -> Dict[str, object]:
        value: Dict[str, object] = {
            "id": self.run_id,
            "number": self.number,
            "attempt": self.attempt,
        }
        if self.commit:
            value["commit"] = self.commit
        return value


def derive_international_url(mainland_url: str) -> str:
    parsed = urlsplit(mainland_url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Mainland manual URL must use http or https")
    if not parsed.hostname:
        raise ValueError("Mainland manual URL must include a hostname")
    if not parsed.hostname.lower().endswith(".cn"):
        raise ValueError("Mainland manual URL hostname must end with .cn")

    host_end = len(parsed.netloc)
    if parsed.port is not None:
        host_end -= len(f":{parsed.port}")
    if parsed.netloc[host_end - 3 : host_end].lower() != ".cn":
        raise ValueError("Mainland manual URL hostname must end with .cn")
    international_netloc = (
        parsed.netloc[: host_end - 3] + ".com" + parsed.netloc[host_end:]
    )
    return urlunsplit(parsed._replace(netloc=international_netloc))


def build_region_targets(
    model_key: str,
    model_name: str,
    mainland_url: str,
    rss_path: Path,
    mainland_pdf_asset: str,
    international_pdf_asset: str,
) -> List[Target]:
    international_url = derive_international_url(mainland_url)
    return [
        Target(
            f"{model_key}_cn",
            model_name,
            mainland_url,
            rss_path,
            mainland_pdf_asset,
            region_code="cn",
            region_name="大陆版",
            legacy_key=model_key,
            feed_url=mainland_url,
        ),
        Target(
            f"{model_key}_com",
            model_name,
            international_url,
            rss_path,
            international_pdf_asset,
            region_code="com",
            region_name="国际版",
            feed_url=mainland_url,
        ),
    ]


def configured_targets() -> List[Target]:
    targets: List[Target] = []
    targets.extend(
        build_region_targets(
            "model3",
            "Model 3",
            required_env("MODEL3_MANUAL_URL"),
            Path(required_env("MODEL3_MANUAL_RSS_PATH")),
            "model3-manual-current.pdf",
            "model3-international-manual-current.pdf",
        )
    )
    targets.extend(
        build_region_targets(
            "modely",
            "Model Y",
            required_env("MODELY_MANUAL_URL"),
            Path(required_env("MODELY_MANUAL_RSS_PATH")),
            "modely-manual-current.pdf",
            "modely-international-manual-current.pdf",
        )
    )
    return targets


class BrowserFetcher:
    """Download a PDF through Chrome for source-collection callers.

    The monitor entry point never constructs this class: it consumes PDFs already
    transported in a workflow artifact.  It remains here as a shared downloader
    for the producer and the local diff demo.
    """

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
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_argument("--lang=zh-CN")
            options.add_argument("--window-size=1920x1080")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option("useAutomationExtension", False)
            options.add_experimental_option(
                "prefs",
                {
                    "download.prompt_for_download": False,
                    "download.directory_upgrade": True,
                    "plugins.always_open_pdf_externally": True,
                },
            )
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            options.page_load_strategy = "eager"
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(self.timeout)
            user_agent = self.driver.execute_script("return navigator.userAgent")
            browser_version = self.driver.capabilities["browserVersion"]
            major_version = browser_version.split(".", 1)[0]
            system = platform.system()
            if system == "Darwin":
                client_platform = "macOS"
                platform_version = "10.15.7"
            elif system == "Windows":
                client_platform = "Windows"
                platform_version = "10.0.0"
            else:
                client_platform = "Linux"
                platform_version = ""
            architecture = "arm" if "arm" in platform.machine().lower() else "x86"
            brands = [
                {"brand": "Not_A Brand", "version": "99"},
                {"brand": "Chromium", "version": major_version},
                {"brand": "Google Chrome", "version": major_version},
            ]
            self.driver.execute_cdp_cmd(
                "Network.setUserAgentOverride",
                {
                    "userAgent": user_agent.replace("HeadlessChrome", "Chrome"),
                    "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
                    "platform": client_platform,
                    "userAgentMetadata": {
                        "brands": brands,
                        "fullVersionList": [
                            {"brand": "Not_A Brand", "version": "99.0.0.0"},
                            {"brand": "Chromium", "version": browser_version},
                            {"brand": "Google Chrome", "version": browser_version},
                        ],
                        "fullVersion": browser_version,
                        "platform": client_platform,
                        "platformVersion": platform_version,
                        "architecture": architecture,
                        "model": "",
                        "mobile": False,
                        "bitness": "64",
                        "wow64": False,
                    },
                },
            )
            self.driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": (
                        "Object.defineProperty(navigator, 'webdriver', "
                        "{get: () => undefined});"
                    )
                },
            )
        return self.driver

    def download(self, url: str) -> bytes:
        driver = self._driver()
        with tempfile.TemporaryDirectory(prefix="manual-browser-download-") as directory:
            driver.execute_cdp_cmd(
                "Browser.setDownloadBehavior",
                {
                    "behavior": "allow",
                    "downloadPath": directory,
                    "eventsEnabled": True,
                },
            )
            driver.get_log("performance")
            navigation = driver.execute_cdp_cmd("Page.navigate", {"url": url})
            deadline = time.monotonic() + self.timeout
            directory_path = Path(directory)
            responses = []
            while time.monotonic() < deadline:
                completed_files = [
                    path
                    for path in directory_path.iterdir()
                    if path.is_file()
                    and not path.name.endswith((".crdownload", ".tmp"))
                ]
                for path in completed_files:
                    content = path.read_bytes()
                    if content.startswith(b"%PDF-"):
                        return content
                responses.extend(browser_download_responses(driver, url))
                if any(browser_response_is_failure(response) for response in responses):
                    break
                time.sleep(0.25)
            observed = sorted(path.name for path in directory_path.iterdir())
            details = browser_download_details(driver, url, navigation, responses)
        raise RuntimeError(
            "Browser PDF download failed; "
            f"observed files: {observed}; {details}"
        )

    def close(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None


def browser_download_responses(driver, requested_url: str) -> List[Dict[str, object]]:
    responses = []
    try:
        for entry in driver.get_log("performance"):
            message = json.loads(entry.get("message", "{}"))
            event = message.get("message", {})
            if event.get("method") != "Network.responseReceived":
                continue
            response = event.get("params", {}).get("response", {})
            response_url = str(response.get("url", ""))
            if urls_equivalent(response_url, requested_url):
                responses.append(response)
    except Exception:
        pass
    return responses


def browser_response_is_failure(response: Dict[str, object]) -> bool:
    try:
        status = int(float(response.get("status", 0)))
    except (TypeError, ValueError):
        status = 0
    mime_type = str(response.get("mimeType", "")).lower()
    return status >= 400 or (status in {200, 206} and mime_type == "text/html")


def browser_download_details(
    driver,
    requested_url: str,
    navigation: object,
    responses: Optional[List[Dict[str, object]]] = None,
) -> str:
    responses = list(responses or [])
    responses.extend(browser_download_responses(driver, requested_url))
    response = responses[-1] if responses else {}
    status = response.get("status", "unknown")
    mime_type = response.get("mimeType", "unknown")
    final_url = str(response.get("url") or getattr(driver, "current_url", ""))
    title = ""
    body = ""
    try:
        title = str(driver.title)
    except Exception:
        pass
    try:
        from selenium.webdriver.common.by import By

        body = normalize_text(driver.find_element(By.TAG_NAME, "body").text)[:300]
    except Exception:
        pass
    navigation_error = ""
    if isinstance(navigation, dict) and navigation.get("errorText"):
        navigation_error = str(navigation["errorText"])
    return (
        f"status={status!r}, mime_type={mime_type!r}, "
        f"final_url={final_url!r}, title={title!r}, body={body!r}, "
        f"navigation_error={navigation_error!r}"
    )


def normalize_text(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.replace(":", "：")).strip()
    return re.sub(r"：\s*", "：", normalized)


def urls_equivalent(left: str, right: str) -> bool:
    try:
        left_url = urlsplit(left)
        right_url = urlsplit(right)
        left_port = left_url.port or (
            443 if left_url.scheme.lower() == "https" else 80
        )
        right_port = right_url.port or (
            443 if right_url.scheme.lower() == "https" else 80
        )

        def normalized_path(value: str) -> str:
            normalized = value.rstrip("/") or "/"
            if normalized.lower().endswith("/index.html"):
                normalized = normalized[: -len("/index.html")] or "/"
            return normalized

        return (
            left_url.scheme.lower(),
            left_url.hostname.lower() if left_url.hostname else None,
            left_port,
            normalized_path(left_url.path),
            left_url.query,
        ) == (
            right_url.scheme.lower(),
            right_url.hostname.lower() if right_url.hostname else None,
            right_port,
            normalized_path(right_url.path),
            right_url.query,
        )
    except ValueError:
        return False


def manual_pdf_url(page_url: str) -> str:
    parsed = urlsplit(page_url)
    path = parsed.path
    if path.lower().endswith("/index.html"):
        path = path[: -len("index.html")] + MANUAL_PDF_FILENAME
    else:
        path = f"{path.rstrip('/')}/{MANUAL_PDF_FILENAME}"
    return urlunsplit(parsed._replace(path=path, query="", fragment=""))


def extracted_version_matches(content: str, version_text_prefix: str) -> List[str]:
    prefix = normalize_text(version_text_prefix)
    matches = set()
    pattern = re.compile(re.escape(prefix) + r"\s*([0-9]+(?:\.[0-9]+)+)")
    for line in content.splitlines():
        visible = re.sub(r"!\[[^]]*\]\([^)]*\)", "", line)
        visible = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", visible)
        visible = re.sub(r"^\s*(?:#{1,6}\s+|>\s+|[-+*]\s+)", "", visible)
        visible = html.unescape(visible).strip(" \t*_`~|")
        normalized = normalize_text(visible)
        for match in pattern.finditer(normalized):
            matches.add(f"{prefix}{match.group(1)}")
    return sorted(matches)


def parse_extracted_manual_page(
    content: str,
    page_url: str,
    version_text_prefix: str,
) -> Tuple[str, str]:
    prefix = normalize_text(version_text_prefix)
    matches = extracted_version_matches(content, version_text_prefix)
    if len(matches) != 1:
        raise ValueError(
            f"Expected exactly one extracted line starting with {prefix!r}; "
            f"found {len(matches)}"
        )

    markdown_targets = re.findall(
        r"\[[^]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))",
        content,
    )
    link_values = [left or right for left, right in markdown_targets]
    link_values.extend(
        value.rstrip(".,;:'\"")
        for value in re.findall(r"https?://[^\s<>()\]]+", content)
    )
    pdf_urls = sorted(
        {
            urljoin(page_url, html.unescape(value))
            for value in link_values
            if urlsplit(urljoin(page_url, html.unescape(value))).path.lower().endswith(
                ".pdf"
            )
        }
    )
    if len(pdf_urls) != 1:
        raise ValueError(
            f"Expected exactly one PDF link in extracted content; found {len(pdf_urls)}"
        )
    return matches[0], pdf_urls[0]


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    has_text = False
    for number, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        has_text = has_text or bool(text.strip())
        pages.append(f"\n===== Page {number} =====\n{text.strip()}")
    return "\n".join(pages).strip() if has_text else ""


def extract_pdf_cover_text(path: Path) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        if not reader.pages:
            raise RuntimeError("PDF has no pages")
        return reader.pages[0].extract_text() or ""
    except RuntimeError:
        raise
    except Exception as error:
        raise RuntimeError(
            f"Could not read candidate PDF cover: {type(error).__name__}: {error}"
        ) from None


def extract_pdf_cover_version(path: Path, version_text_prefix: str) -> str:
    prefix = normalize_text(version_text_prefix)
    cover_text = normalize_text(html.unescape(extract_pdf_cover_text(path)))
    pattern = re.compile(re.escape(prefix) + r"([0-9]+(?:\.[0-9]+)+)")
    matches = [f"{prefix}{match.group(1)}" for match in pattern.finditer(cover_text)]
    if len(matches) != 1:
        raise RuntimeError(
            f"PDF cover expected exactly one version starting with {prefix!r}; "
            f"found {len(matches)}"
        )
    return matches[0]


def make_text_diff(old_text: str, new_text: str) -> Tuple[str, int, int]:
    if not old_text.strip() and not new_text.strip():
        return "PDF 无可提取的文本层，未执行 OCR。", 0, 0
    text_layer_note = ""
    if not old_text.strip():
        text_layer_note = "上一版本 PDF 无可提取的文本层，未执行 OCR。\n"
    elif not new_text.strip():
        text_layer_note = "当前版本 PDF 无可提取的文本层，未执行 OCR。\n"
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
    diff_text = "\n".join(lines) or "PDF 可提取文字没有差异。"
    return text_layer_note + diff_text, additions, deletions


def fallback_summary(old_version: str, new_version: str, additions: int, deletions: int) -> str:
    return (
        "Gemini 总结失败，以下为基础差异统计。"
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
    if not isinstance(payload, Mapping):
        raise RuntimeError("Bark returned an invalid JSON response")
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
    except (
        requests.RequestException,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
    ) as error:
        raise RuntimeError(f"Gemini summary failed: {type(error).__name__}") from None
    if not summary:
        raise RuntimeError("Gemini summary failed: empty response")
    return summary


def load_state(path: Path) -> Dict[str, object]:
    if not path.exists():
        return {"schema_version": STATE_SCHEMA_VERSION, "targets": {}}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") == 1:
        legacy_targets = state.get("targets")
        if not isinstance(legacy_targets, dict):
            raise RuntimeError("Invalid manual state file")
        migrated_targets = dict(legacy_targets)
        for legacy_key, mainland_key in (
            ("model3", "model3_cn"),
            ("modely", "modely_cn"),
        ):
            if legacy_key in migrated_targets and mainland_key not in migrated_targets:
                migrated_targets[mainland_key] = migrated_targets[legacy_key]
            migrated_targets.pop(legacy_key, None)
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "targets": migrated_targets,
        }
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


def _positive_int(value: object, field: str, context: str = "Candidate manifest") -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{context} {field} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise RuntimeError(
            f"{context} {field} must be a positive integer"
        ) from None
    if number <= 0 or str(value).strip() != str(number):
        raise RuntimeError(f"{context} {field} must be a positive integer")
    return number


def _source_run_from_mapping(value: object, context: str) -> SourceRun:
    if not isinstance(value, Mapping):
        raise RuntimeError(f"{context} source_run must be an object")
    commit_value = value.get("commit")
    if commit_value is not None and (
        not isinstance(commit_value, str) or not commit_value.strip()
    ):
        raise RuntimeError(f"{context} source_run.commit must be a non-empty string")
    return SourceRun(
        run_id=_positive_int(value.get("id"), "source_run.id", context),
        number=_positive_int(value.get("number"), "source_run.number", context),
        attempt=_positive_int(value.get("attempt"), "source_run.attempt", context),
        commit=commit_value.strip() if isinstance(commit_value, str) else None,
    )


def _validate_source_run_environment(source_run: SourceRun) -> None:
    expected_values = {
        "MANUAL_SOURCE_RUN_ID": str(source_run.run_id),
        "MANUAL_SOURCE_RUN_NUMBER": str(source_run.number),
        "MANUAL_SOURCE_RUN_ATTEMPT": str(source_run.attempt),
        "MANUAL_SOURCE_COMMIT": source_run.commit or "",
    }
    for name, actual in expected_values.items():
        expected = os.environ.get(name, "").strip()
        if expected and expected != actual:
            raise RuntimeError(
                f"Candidate manifest source run does not match {name}"
            )


def load_candidate_manifest(
    candidate_dir: Path,
) -> Tuple[SourceRun, Dict[str, Mapping[str, object]]]:
    manifest_path = candidate_dir / CANDIDATE_MANIFEST_NAME
    if not manifest_path.is_file():
        raise RuntimeError(f"Candidate manifest is missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Invalid candidate manifest: {type(error).__name__}: {error}"
        ) from None
    if not isinstance(manifest, Mapping):
        raise RuntimeError("Candidate manifest must be an object")
    if manifest.get("schema_version") != CANDIDATE_SCHEMA_VERSION:
        raise RuntimeError("Unsupported candidate manifest schema version")
    source_run = _source_run_from_mapping(
        manifest.get("source_run"), "Candidate manifest"
    )
    _validate_source_run_environment(source_run)
    entries = manifest.get("candidates")
    if not isinstance(entries, list):
        raise RuntimeError("Candidate manifest candidates must be an array")
    by_target: Dict[str, Mapping[str, object]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise RuntimeError(f"Candidate entry {index} must be an object")
        target_key = entry.get("target_key")
        if not isinstance(target_key, str) or not target_key.strip():
            raise RuntimeError(
                f"Candidate entry {index} target_key must be a non-empty string"
            )
        target_key = target_key.strip()
        if target_key in by_target:
            raise RuntimeError(f"Duplicate candidate target: {target_key}")
        by_target[target_key] = entry
    return source_run, by_target


def _required_candidate_string(
    entry: Mapping[str, object], field: str, target: Target
) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"{target.display_name}: candidate {field} must be a non-empty string"
        )
    if value != value.strip():
        raise RuntimeError(
            f"{target.display_name}: candidate {field} must not have outer whitespace"
        )
    return value


def _validate_candidate_pdf_url(target: Target, pdf_url: str) -> None:
    expected = manual_pdf_url(target.page_url)
    parsed = urlsplit(pdf_url)
    expected_parsed = urlsplit(expected)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"{target.display_name}: candidate PDF URL is invalid")
    try:
        parsed_port = parsed.port or (
            443 if parsed.scheme.lower() == "https" else 80
        )
        expected_port = expected_parsed.port or (
            443 if expected_parsed.scheme.lower() == "https" else 80
        )
    except ValueError as error:
        raise RuntimeError(
            f"{target.display_name}: candidate PDF URL has an invalid port"
        ) from error
    if parsed.fragment:
        raise RuntimeError(
            f"{target.display_name}: candidate PDF URL does not match its region"
        )
    if (
        parsed.scheme.lower(),
        parsed.hostname.lower(),
        parsed_port,
    ) != (
        expected_parsed.scheme.lower(),
        expected_parsed.hostname.lower(),
        expected_port,
    ):
        raise RuntimeError(
            f"{target.display_name}: candidate PDF URL origin does not match its region"
        )
    if not parsed.path.endswith(f"/{MANUAL_PDF_FILENAME}"):
        raise RuntimeError(
            f"{target.display_name}: candidate PDF URL does not name the manual PDF"
        )
    suffix = f".{target.region_code}"
    if not parsed.hostname.lower().endswith(suffix):
        raise RuntimeError(
            f"{target.display_name}: candidate PDF URL has the wrong region"
        )
    if target.region_code == "com" and not urls_equivalent(pdf_url, expected):
        raise RuntimeError(
            f"{target.display_name}: candidate PDF URL does not match its region"
        )
    if target.region_code not in {"cn", "com"}:
        raise RuntimeError(
            f"{target.display_name}: candidate PDF URL has an unsupported region"
        )


def validate_candidate(
    candidate_dir: Path,
    entry: Mapping[str, object],
    target: Target,
    version_text_prefix: str,
) -> PdfCandidate:
    target_key = _required_candidate_string(entry, "target_key", target)
    version = _required_candidate_string(entry, "version", target)
    pdf_url = _required_candidate_string(entry, "pdf_url", target)
    pdf_sha256 = _required_candidate_string(entry, "pdf_sha256", target)
    pdf_asset = _required_candidate_string(entry, "pdf_asset", target)
    candidate_file = _required_candidate_string(entry, "candidate_file", target)
    version_source = _required_candidate_string(entry, "version_source", target)
    rss_guid = _required_candidate_string(entry, "rss_guid", target)
    mode = _required_candidate_string(entry, "mode", target)

    if target_key != target.key:
        raise RuntimeError(f"{target.display_name}: candidate target_key mismatch")
    normalized_version = normalize_text(version)
    if normalized_version != version or extracted_version_matches(
        version, version_text_prefix
    ) != [version]:
        raise RuntimeError(f"{target.display_name}: candidate version is invalid")
    if pdf_asset != target.pdf_asset or candidate_file != target.pdf_asset:
        raise RuntimeError(
            f"{target.display_name}: candidate PDF asset name is not the fixed asset"
        )
    if Path(candidate_file).name != candidate_file:
        raise RuntimeError(f"{target.display_name}: unsafe candidate file name")
    expected_source = "html+pdf" if target.region_code == "cn" else "pdf"
    if version_source != expected_source:
        raise RuntimeError(
            f"{target.display_name}: candidate version_source must be "
            f"{expected_source!r}"
        )
    if mode not in {"pending", "reconcile"}:
        raise RuntimeError(f"{target.display_name}: candidate mode is invalid")
    expected_guid = target.guid_for(version)
    legacy_guid = (
        f"{target.legacy_guid_prefix}{version}"
        if target.legacy_guid_prefix
        else None
    )
    allowed_guids = {expected_guid}
    if mode == "reconcile" and legacy_guid:
        allowed_guids.add(legacy_guid)
    if rss_guid not in allowed_guids:
        raise RuntimeError(
            f"{target.display_name}: candidate RSS GUID does not match its region/version"
        )
    _validate_candidate_pdf_url(target, pdf_url)
    if not re.fullmatch(r"[0-9a-f]{64}", pdf_sha256):
        raise RuntimeError(f"{target.display_name}: candidate SHA-256 is invalid")
    pdf_size = entry.get("pdf_size")
    if isinstance(pdf_size, bool) or not isinstance(pdf_size, int) or pdf_size <= 0:
        raise RuntimeError(f"{target.display_name}: candidate PDF size is invalid")

    path = candidate_dir / candidate_file
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{target.display_name}: candidate PDF file is missing")
    actual_size = path.stat().st_size
    if actual_size != pdf_size:
        raise RuntimeError(
            f"{target.display_name}: candidate PDF size mismatch "
            f"({actual_size} != {pdf_size})"
        )
    with path.open("rb") as source:
        if source.read(5) != b"%PDF-":
            raise RuntimeError(f"{target.display_name}: candidate is not a PDF")
    actual_hash = sha256_file(path)
    if actual_hash != pdf_sha256:
        raise RuntimeError(f"{target.display_name}: candidate SHA-256 mismatch")
    cover_version = extract_pdf_cover_version(path, version_text_prefix)
    if cover_version != version:
        raise RuntimeError(
            f"{target.display_name}: candidate PDF version {cover_version!r} "
            f"does not match manifest version {version!r}"
        )
    return PdfCandidate(
        target_key=target_key,
        version=version,
        pdf_url=pdf_url,
        pdf_sha256=pdf_sha256,
        pdf_size=pdf_size,
        pdf_asset=pdf_asset,
        candidate_file=candidate_file,
        version_source=version_source,
        rss_guid=rss_guid,
        mode=mode,
        path=path,
    )


def rss_description(
    target_name: str,
    old_version: str,
    new_version: str,
    summary: str,
    additions: int,
    deletions: int,
) -> str:
    return (
        f"检查目标：{target_name}\n旧版本：{old_version}\n新版本：{new_version}\n"
        f"PDF 文本变化：新增 {additions} 行，删除 {deletions} 行\n\n{summary}"
    )


def rss_entry_by_guid(path: Path, guid_value: str) -> Optional[RssEntry]:
    if not path.exists():
        return None
    root = ET.parse(path).getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError(f"Invalid RSS file: {path}")
    matching = [
        item
        for item in channel.findall("item")
        if (item.findtext("guid") or "").strip() == guid_value
    ]
    if len(matching) > 1:
        raise RuntimeError(f"Duplicate RSS GUID {guid_value!r}: {path}")
    if not matching:
        return None
    item = matching[0]
    return RssEntry(
        title=(item.findtext("title") or "").strip(),
        pdf_url=(item.findtext("link") or "").strip(),
        guid=guid_value,
        description=(item.findtext("description") or "").strip(),
    )


def version_from_target_guid(target: Target, guid_value: str) -> Optional[str]:
    if guid_value.startswith(target.guid_prefix):
        return guid_value[len(target.guid_prefix) :].strip()
    if target.legacy_guid_prefix and guid_value.startswith(
        target.legacy_guid_prefix
    ):
        suffix = guid_value[len(target.legacy_guid_prefix) :].strip()
        # The legacy prefix is also a lexical prefix of all region-aware GUIDs.
        # Only the historical, region-less form belongs to the mainland target.
        if suffix.startswith(("cn-", "com-")):
            return None
        return suffix
    return None


def pending_rss_guids(path: Path, target: Target) -> List[str]:
    if not path.exists():
        return []
    root = ET.parse(path).getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError(f"Invalid RSS file: {path}")
    values = []
    for item in channel.findall("item"):
        guid = (item.findtext("guid") or "").strip()
        if version_from_target_guid(target, guid) is None:
            continue
        description = (item.findtext("description") or "").strip()
        if not description:
            values.append(guid)
    return values


def pending_rss_entry(
    path: Path, target: Optional[Target] = None
) -> Optional[Tuple[str, str, str]]:
    if not path.exists():
        return None
    root = ET.parse(path).getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError(f"Invalid RSS file: {path}")
    for item in channel.findall("item"):
        description = item.find("description")
        if description is not None and (description.text or "").strip():
            continue
        title = (item.findtext("title") or "").strip()
        pdf_url = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or "").strip()
        version = title
        if target is not None:
            guid_version = version_from_target_guid(target, guid)
            if guid_version is None:
                continue
            is_legacy = (
                target.legacy_guid_prefix
                and guid.startswith(target.legacy_guid_prefix)
                and not guid.startswith(target.guid_prefix)
            )
            if is_legacy:
                legacy_version = guid_version
                if legacy_version != title:
                    continue
                version = legacy_version
            else:
                version = guid_version
        if not version or not title or not pdf_url or not guid:
            raise RuntimeError(f"Incomplete pending RSS item: {path}")
        return version, pdf_url, guid
    return None


def complete_rss_entry(path: Path, guid_value: str, description_text: str) -> None:
    tree = ET.parse(path)
    root = tree.getroot()
    channel = root.find("channel")
    if channel is None:
        raise RuntimeError(f"Invalid RSS file: {path}")
    for item in channel.findall("item"):
        if (item.findtext("guid") or "").strip() != guid_value:
            continue
        description = item.find("description")
        if description is None:
            description = ET.SubElement(item, "description")
        description.text = description_text
        ET.indent(root, space="  ")
        tree.write(path, encoding="utf-8", xml_declaration=True)
        return
    raise RuntimeError(f"Pending RSS item disappeared: {guid_value}")


def _validate_rss_for_candidate(target: Target, candidate: PdfCandidate) -> RssEntry:
    entry = rss_entry_by_guid(target.rss_path, candidate.rss_guid)
    if entry is None:
        raise RuntimeError(
            f"{target.display_name}: candidate RSS GUID does not exist"
        )
    guid_version = version_from_target_guid(target, entry.guid)
    if guid_version != candidate.version:
        raise RuntimeError(
            f"{target.display_name}: RSS GUID version does not match candidate"
        )
    expected_title = target.item_title(candidate.version)
    legacy_title = candidate.version if entry.guid != target.guid_for(candidate.version) else None
    if entry.title not in {expected_title, legacy_title}:
        raise RuntimeError(
            f"{target.display_name}: RSS title does not match candidate version/region"
        )
    if not urls_equivalent(entry.pdf_url, candidate.pdf_url):
        raise RuntimeError(
            f"{target.display_name}: RSS PDF URL does not match candidate"
        )
    pending_guids = pending_rss_guids(target.rss_path, target)
    if candidate.mode == "pending":
        if not entry.description and pending_guids != [candidate.rss_guid]:
            raise RuntimeError(
                f"{target.display_name}: RSS does not have exactly the candidate "
                "as its pending entry"
            )
    else:
        if not entry.description:
            raise RuntimeError(
                f"{target.display_name}: reconcile RSS entry must already be complete"
            )
        if pending_guids:
            raise RuntimeError(
                f"{target.display_name}: reconcile refused while RSS has a pending entry"
            )
    return entry


def _state_matches_candidate(
    previous: object, target: Target, candidate: PdfCandidate
) -> bool:
    return bool(
        isinstance(previous, Mapping)
        and previous.get("version") == candidate.version
        and previous.get("pdf_sha256") == candidate.pdf_sha256
        and previous.get("pdf_asset") == target.pdf_asset
        and isinstance(previous.get("pdf_url"), str)
        and urls_equivalent(str(previous.get("pdf_url")), candidate.pdf_url)
    )


def _previous_source_run(previous: object) -> Optional[SourceRun]:
    if not isinstance(previous, Mapping) or previous.get("source_run") is None:
        return None
    return _source_run_from_mapping(previous.get("source_run"), "Manual state")


def _reject_stale_source(previous: object, source_run: SourceRun) -> None:
    old_source = _previous_source_run(previous)
    if old_source is None:
        return
    if source_run.order < old_source.order:
        raise RuntimeError(
            "Candidate source run is older than the installed target baseline"
        )
    if source_run.order == old_source.order and (
        source_run.run_id != old_source.run_id
        or source_run.commit != old_source.commit
    ):
        raise RuntimeError(
            "Candidate source run conflicts with the installed target baseline"
        )


def _validate_installed_baseline(
    previous: object,
    target: Target,
    state_dir: Path,
    version_text_prefix: str,
) -> Path:
    if not isinstance(previous, Mapping):
        raise RuntimeError(f"{target.display_name}: invalid previous target state")
    version = previous.get("version")
    pdf_url = previous.get("pdf_url")
    pdf_hash = previous.get("pdf_sha256")
    pdf_asset = previous.get("pdf_asset")
    if not isinstance(version, str) or not version:
        raise RuntimeError(f"{target.display_name}: previous version is missing")
    if not isinstance(pdf_url, str) or not pdf_url:
        raise RuntimeError(f"{target.display_name}: previous PDF URL is missing")
    _validate_candidate_pdf_url(target, pdf_url)
    if pdf_asset != target.pdf_asset:
        raise RuntimeError(
            f"{target.display_name}: previous PDF asset does not match its region"
        )
    if not isinstance(pdf_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", pdf_hash):
        raise RuntimeError(f"{target.display_name}: previous PDF SHA-256 is invalid")
    asset_path = state_dir / target.pdf_asset
    if not asset_path.is_file() or asset_path.is_symlink():
        raise RuntimeError(f"Previous PDF asset is missing: {target.pdf_asset}")
    with asset_path.open("rb") as source:
        if source.read(5) != b"%PDF-":
            raise RuntimeError(
                f"{target.display_name}: previous PDF asset is not a PDF"
            )
    if sha256_file(asset_path) != pdf_hash:
        raise RuntimeError(
            f"{target.display_name}: previous PDF asset SHA-256 mismatch"
        )
    actual_version = extract_pdf_cover_version(asset_path, version_text_prefix)
    if actual_version != version:
        raise RuntimeError(
            f"{target.display_name}: previous PDF version {actual_version!r} "
            f"does not match state version {version!r}"
        )
    return asset_path


def _state_record(
    target: Target,
    candidate: PdfCandidate,
    source_run: SourceRun,
    description: str,
) -> Dict[str, object]:
    return {
        "version": candidate.version,
        "pdf_url": candidate.pdf_url,
        "pdf_sha256": candidate.pdf_sha256,
        "pdf_size": candidate.pdf_size,
        "pdf_asset": target.pdf_asset,
        "version_source": candidate.version_source,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "rss_description": description,
        "source_run": source_run.as_state(),
    }


def _install_candidate_and_update_rss(
    target: Target,
    candidate: PdfCandidate,
    asset_path: Path,
    description: Optional[str],
) -> None:
    import shutil

    asset_path.parent.mkdir(parents=True, exist_ok=True)
    rss_original = target.rss_path.read_bytes()
    staged_file = tempfile.NamedTemporaryFile(
        prefix=f".{target.pdf_asset}.", suffix=".new", dir=asset_path.parent,
        delete=False,
    )
    staged_path = Path(staged_file.name)
    staged_file.close()
    backup_path: Optional[Path] = None
    try:
        shutil.copyfile(candidate.path, staged_path)
        if sha256_file(staged_path) != candidate.pdf_sha256:
            raise RuntimeError(
                f"{target.display_name}: staged candidate SHA-256 mismatch"
            )
        if asset_path.exists():
            backup_file = tempfile.NamedTemporaryFile(
                prefix=f".{target.pdf_asset}.", suffix=".old",
                dir=asset_path.parent, delete=False,
            )
            backup_path = Path(backup_file.name)
            backup_file.close()
            backup_path.unlink()
            asset_path.replace(backup_path)
        staged_path.replace(asset_path)
        if description is not None:
            complete_rss_entry(target.rss_path, candidate.rss_guid, description)
    except Exception:
        try:
            target.rss_path.write_bytes(rss_original)
        except OSError:
            pass
        try:
            if asset_path.exists():
                asset_path.unlink()
            if backup_path is not None and backup_path.exists():
                backup_path.replace(asset_path)
        finally:
            if staged_path.exists():
                staged_path.unlink()
        raise
    else:
        if backup_path is not None and backup_path.exists():
            backup_path.unlink()


def process_target(
    session: requests.Session,
    target: Target,
    state: Dict[str, object],
    state_dir: Path,
    candidate: PdfCandidate,
    source_run: SourceRun,
    version_text_prefix: str,
    gemini_api_key: str,
    gemini_model: str,
    timeout: int,
    bark_base_url: str,
    bark_token: str,
    bark_title: str,
    bark_group: str,
) -> str:
    targets_state = state["targets"]
    previous = targets_state.get(target.key)
    rss_entry = _validate_rss_for_candidate(target, candidate)
    _reject_stale_source(previous, source_run)
    asset_path = state_dir / target.pdf_asset

    if rss_entry.description:
        if candidate.mode == "reconcile":
            if _state_matches_candidate(previous, target, candidate):
                _validate_installed_baseline(
                    previous, target, state_dir, version_text_prefix
                )
                print(
                    f"{target.display_name}: candidate already reconciled "
                    f"({candidate.version})"
                )
                return "already_processed"
            description = rss_entry.description
            _install_candidate_and_update_rss(
                target, candidate, asset_path, description=None
            )
            targets_state[target.key] = _state_record(
                target, candidate, source_run, description
            )
            old_version = (
                previous.get("version") if isinstance(previous, Mapping) else "none"
            )
            print(
                f"{target.display_name}: reconciled baseline "
                f"{old_version} -> {candidate.version} without notification"
            )
            return "reconciled"

        if not _state_matches_candidate(previous, target, candidate):
            raise RuntimeError(
                f"{target.display_name}: completed pending RSS entry does not "
                "match installed state; use reconcile mode"
            )
        _validate_installed_baseline(
            previous, target, state_dir, version_text_prefix
        )
        print(
            f"{target.display_name}: candidate was already processed "
            f"({candidate.version})"
        )
        return "already_processed"

    if candidate.mode != "pending":
        raise RuntimeError(
            f"{target.display_name}: reconcile candidate unexpectedly has a "
            "pending RSS entry"
        )

    if previous is not None and previous.get("version") == candidate.version:
        if not _state_matches_candidate(previous, target, candidate):
            raise RuntimeError(
                f"{target.display_name}: same-version candidate does not match "
                "installed state; use reconcile mode"
            )
        _validate_installed_baseline(
            previous, target, state_dir, version_text_prefix
        )
        description = previous.get("rss_description") or (
            f"{candidate.version} 已建立 PDF 基线，本次未重复比较。"
        )
        complete_rss_entry(target.rss_path, candidate.rss_guid, description)
        previous["rss_description"] = description
        previous["source_run"] = source_run.as_state()
        print(
            f"{target.display_name}: restored RSS description for "
            f"{candidate.version} after validating the artifact"
        )
        return "recovered"

    if previous is None:
        description = (
            f"首次记录 {target.display_name} {candidate.version}，已建立 PDF 基线，"
            "暂无上一版本可供比较。"
        )
        _install_candidate_and_update_rss(
            target, candidate, asset_path, description
        )
        targets_state[target.key] = _state_record(
            target, candidate, source_run, description
        )
        try:
            send_bark(
                session,
                bark_base_url,
                bark_token,
                f"{bark_title} - {target.display_name}",
                description,
                candidate.pdf_url,
                bark_group,
                timeout,
            )
        except RuntimeError as error:
            print(f"::warning::{target.display_name}: {error}")
        print(
            f"{target.display_name}: baseline created and RSS completed "
            f"({candidate.version})"
        )
        return "baseline"

    asset_path = _validate_installed_baseline(
        previous, target, state_dir, version_text_prefix
    )
    old_text = extract_pdf_text(asset_path)
    new_text = extract_pdf_text(candidate.path)
    diff_text, additions, deletions = make_text_diff(old_text, new_text)

    old_version = previous["version"]
    try:
        summary = summarize_with_gemini(
            session,
            gemini_api_key,
            gemini_model,
            target.display_name,
            old_version,
            candidate.version,
            diff_text,
            additions,
            deletions,
            timeout,
        )
    except RuntimeError as error:
        print(f"::warning::{target.display_name}: {error}")
        summary = fallback_summary(
            old_version, candidate.version, additions, deletions
        )

    description = rss_description(
        target.display_name,
        old_version,
        candidate.version,
        summary,
        additions,
        deletions,
    )
    _install_candidate_and_update_rss(
        target, candidate, asset_path, description
    )
    targets_state[target.key] = _state_record(
        target, candidate, source_run, description
    )
    try:
        send_bark(
            session,
            bark_base_url,
            bark_token,
            f"{bark_title} - {target.display_name}",
            bark_body(
                old_version, candidate.version, additions, deletions, summary
            ),
            candidate.pdf_url,
            bark_group,
            timeout,
        )
        print(f"{target.display_name}: Bark notification sent")
    except RuntimeError as error:
        print(f"::warning::{target.display_name}: {error}")
    print(
        f"{target.display_name}: updated {old_version} -> {candidate.version}"
    )
    return "updated"


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable is empty: {name}")
    return value


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process pre-fetched owner-manual PDF candidates."
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    return parser.parse_args(argv)


def write_monitor_result(
    path: Path,
    results: Mapping[str, str],
    errors: Sequence[str],
    state_saved: bool,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "results": dict(results),
                "errors": list(errors),
                "state_saved": state_saved,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    state_path = args.state_dir / "manual-version-state.json"
    errors: List[str] = []
    results: Dict[str, str] = {}
    try:
        state = load_state(state_path)
        targets = configured_targets()
        source_run, candidates = load_candidate_manifest(args.candidate_dir)
        version_text_prefix = required_env("MANUAL_VERSION_TEXT_PREFIX")
        gemini_model = required_env("GEMINI_MODEL")
        bark_base_url = required_env("BARK_BASE_URL")
        bark_title = required_env("MANUAL_BARK_TITLE")
        bark_group = required_env("MANUAL_BARK_GROUP")
        timeout = int(os.environ.get("MANUAL_REQUEST_TIMEOUT_SECONDS", "30"))
        if timeout <= 0:
            raise RuntimeError("MANUAL_REQUEST_TIMEOUT_SECONDS must be positive")
    except Exception as error:
        message = f"Configuration: {type(error).__name__}: {error}"
        print(f"::error::{message}")
        write_monitor_result(args.result_file, results, [message], False)
        return 1

    target_keys = {target.key for target in targets}
    unknown_keys = sorted(set(candidates) - target_keys)
    if unknown_keys:
        message = "Candidate manifest has unknown targets: " + ", ".join(unknown_keys)
        print(f"::error::{message}")
        errors.append(message)

    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    for target in targets:
        raw_candidate = candidates.get(target.key)
        if raw_candidate is None:
            results[target.key] = "unchanged"
            print(f"{target.display_name}: no PDF candidate in artifact")
            continue
        try:
            candidate = validate_candidate(
                args.candidate_dir,
                raw_candidate,
                target,
                version_text_prefix,
            )
            results[target.key] = process_target(
                session,
                target,
                state,
                args.state_dir,
                candidate,
                source_run,
                version_text_prefix,
                os.environ.get("GEMINI_API_KEY", "").strip(),
                gemini_model,
                timeout,
                bark_base_url,
                os.environ.get("BARK_TOKEN", "").strip(),
                bark_title,
                bark_group,
            )
        except Exception as error:
            message = f"{target.display_name}: {type(error).__name__}: {error}"
            print(f"::error::{message}")
            errors.append(message)
            results[target.key] = "failed"

    state_saved = False
    try:
        save_state(state_path, state)
        state_saved = True
    except Exception as error:
        message = f"State: {type(error).__name__}: {error}"
        print(f"::error::{message}")
        errors.append(message)
    write_monitor_result(args.result_file, results, errors, state_saved)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
