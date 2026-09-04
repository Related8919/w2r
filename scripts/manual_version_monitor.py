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
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit

import requests


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
STATE_SCHEMA_VERSION = 2
MAX_GEMINI_DIFF_CHARS = 120000
TAVILY_EXTRACT_DEPTHS = {"basic", "advanced"}
TAVILY_EXTRACT_FORMATS = {"markdown"}
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


class TavilyPageFetcher:
    def __init__(
        self,
        timeout: int,
        api_key: str,
        extract_depth: str = "advanced",
        output_format: str = "markdown",
        client=None,
    ):
        if timeout <= 0:
            raise ValueError("Tavily timeout must be greater than zero")
        if not api_key.strip():
            raise RuntimeError("Required environment variable is empty: TAVILY_API_KEY")
        if extract_depth not in TAVILY_EXTRACT_DEPTHS:
            raise ValueError(
                "Tavily extract depth must be one of: "
                + ", ".join(sorted(TAVILY_EXTRACT_DEPTHS))
            )
        if output_format not in TAVILY_EXTRACT_FORMATS:
            raise ValueError(
                "Tavily extract format must be one of: "
                + ", ".join(sorted(TAVILY_EXTRACT_FORMATS))
            )

        if client is None:
            from tavily import TavilyClient

            client = TavilyClient(api_key=api_key)
        self.timeout = min(float(timeout), 60.0)
        self.api_key = api_key
        self.extract_depth = extract_depth
        self.output_format = output_format
        self.client = client

    def _search_content(
        self, url: str, target_name: str, version_text_prefix: str
    ) -> str:
        parsed_url = urlsplit(url)
        if not parsed_url.hostname:
            raise RuntimeError(f"Manual page URL has no hostname: {url}")
        query = (
            f'"{target_name} 车主手册" '
            f'"{normalize_text(version_text_prefix)}" "China" {url}'
        )
        try:
            response = self.client.search(
                query=query,
                search_depth="advanced",
                max_results=10,
                include_domains=[parsed_url.hostname],
                timeout=self.timeout,
            )
        except Exception as error:
            message = str(error).replace(self.api_key, "<redacted>")
            raise RuntimeError(
                f"Tavily Search request failed for {url}: "
                f"{type(error).__name__}: {message}"
            ) from None

        if not isinstance(response, dict):
            raise RuntimeError("Tavily Search returned a non-object response")
        results = response.get("results", [])
        if not isinstance(results, list):
            raise RuntimeError("Tavily Search returned an invalid results collection")
        matching_results = [
            result
            for result in results
            if isinstance(result, dict)
            and urls_equivalent(str(result.get("url", "")), url)
        ]
        if len(matching_results) != 1:
            raise RuntimeError(
                "Tavily Search expected exactly one matching result for "
                f"{url}; found {len(matching_results)}"
            )
        content = matching_results[0].get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError(f"Tavily Search returned empty content for {url}")
        if len(extracted_version_matches(content, version_text_prefix)) != 1:
            raise RuntimeError(
                "Tavily Search result did not contain exactly one normalized "
                f"version for {url}"
            )
        pdf_url = manual_pdf_url(url)
        print(f"::warning::Tavily Extract failed for {url}; using exact Search result")
        return f"{content}\n\n[Download PDF]({pdf_url})"

    def page_content(
        self, url: str, target_name: str, version_text_prefix: str
    ) -> str:
        extract_error = None
        try:
            response = self.client.extract(
                urls=[url],
                extract_depth=self.extract_depth,
                format=self.output_format,
                timeout=self.timeout,
            )
        except Exception as error:
            message = str(error).replace(self.api_key, "<redacted>")
            extract_error = (
                f"Tavily Extract request failed for {url}: "
                f"{type(error).__name__}: {message}"
            )
            response = None

        if response is not None and not isinstance(response, dict):
            extract_error = "Tavily Extract returned a non-object response"
            response = None

        if response is None:
            try:
                return self._search_content(url, target_name, version_text_prefix)
            except RuntimeError as search_error:
                raise RuntimeError(f"{extract_error}; {search_error}") from None

        results = response.get("results", [])
        failed_results = response.get("failed_results", [])
        if not isinstance(results, list) or not isinstance(failed_results, list):
            extract_error = "Tavily Extract returned invalid result collections"
            try:
                return self._search_content(url, target_name, version_text_prefix)
            except RuntimeError as search_error:
                raise RuntimeError(f"{extract_error}; {search_error}") from None

        matching_results = [
            result
            for result in results
            if isinstance(result, dict)
            and urls_equivalent(str(result.get("url", "")), url)
        ]
        if len(matching_results) != 1:
            failure_messages = [
                str(result.get("error", "unknown error")).replace(
                    self.api_key, "<redacted>"
                )
                for result in failed_results
                if isinstance(result, dict)
                and urls_equivalent(str(result.get("url", "")), url)
            ]
            detail = f"; failure={failure_messages[0]}" if failure_messages else ""
            extract_error = (
                "Tavily Extract expected exactly one matching result for "
                f"{url}; found {len(matching_results)}{detail}"
            )
            try:
                return self._search_content(url, target_name, version_text_prefix)
            except RuntimeError as search_error:
                raise RuntimeError(f"{extract_error}; {search_error}") from None

        content = matching_results[0].get("raw_content")
        if not isinstance(content, str) or not content.strip():
            extract_error = f"Tavily Extract returned empty content for {url}"
            try:
                return self._search_content(url, target_name, version_text_prefix)
            except RuntimeError as search_error:
                raise RuntimeError(f"{extract_error}; {search_error}") from None
        try:
            parse_extracted_manual_page(content, url, version_text_prefix)
        except ValueError as error:
            extract_error = f"Tavily Extract content validation failed: {error}"
            try:
                return self._search_content(url, target_name, version_text_prefix)
            except RuntimeError as search_error:
                raise RuntimeError(f"{extract_error}; {search_error}") from None
        return content

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


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
            if guid.startswith(target.guid_prefix):
                version = guid[len(target.guid_prefix) :].strip()
            elif target.legacy_guid_prefix and guid.startswith(
                target.legacy_guid_prefix
            ):
                legacy_version = guid[len(target.legacy_guid_prefix) :].strip()
                if legacy_version != title:
                    continue
                version = legacy_version
            else:
                continue
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


def process_target(
    session: requests.Session,
    target: Target,
    state: Dict[str, object],
    state_dir: Path,
    version_text_prefix: str,
    gemini_api_key: str,
    gemini_model: str,
    timeout: int,
    page_fetcher: TavilyPageFetcher,
    pdf_fetcher: BrowserFetcher,
    bark_base_url: str,
    bark_token: str,
    bark_title: str,
    bark_group: str,
) -> str:
    targets_state = state["targets"]
    previous = targets_state.get(target.key)
    pending = pending_rss_entry(target.rss_path, target)
    if pending is None:
        print(f"{target.display_name}: no pending RSS entry")
        return "unchanged"
    version, pdf_url, guid_value = pending
    if previous and previous.get("version") == version:
        description = previous.get("rss_description") or (
            f"{version} 已建立 PDF 基线，本次未重复比较。"
        )
        complete_rss_entry(target.rss_path, guid_value, description)
        previous["rss_description"] = description
        print(f"{target.display_name}: restored RSS description for {version}")
        return "recovered"

    page_content = page_fetcher.page_content(
        target.page_url, target.name, version_text_prefix
    )
    version_matches = extracted_version_matches(page_content, version_text_prefix)
    if len(version_matches) != 1:
        prefix = normalize_text(version_text_prefix)
        raise RuntimeError(
            f"Tavily content expected exactly one line starting with {prefix!r}; "
            f"found {len(version_matches)}"
        )
    if version_matches[0] != version:
        raise RuntimeError(
            f"Pending RSS version {version!r} does not match current Tavily "
            f"version {version_matches[0]!r}"
        )
    pdf_content = pdf_fetcher.download(pdf_url)
    if not pdf_content.startswith(b"%PDF-"):
        raise RuntimeError("Downloaded owner manual is not a valid PDF")
    pdf_hash = sha256_bytes(pdf_content)
    detected_at = datetime.now(timezone.utc)
    asset_path = state_dir / target.pdf_asset

    if previous is None:
        description = (
            f"首次记录 {target.display_name} {version}，已建立 PDF 基线，"
            "暂无上一版本可供比较。"
        )
        complete_rss_entry(target.rss_path, guid_value, description)
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(pdf_content)
        targets_state[target.key] = {
            "version": version,
            "pdf_url": pdf_url,
            "pdf_sha256": pdf_hash,
            "pdf_asset": target.pdf_asset,
            "updated_at": detected_at.isoformat(),
            "rss_description": description,
        }
        try:
            send_bark(
                session,
                bark_base_url,
                bark_token,
                f"{bark_title} - {target.display_name}",
                description,
                pdf_url,
                bark_group,
                timeout,
            )
        except RuntimeError as error:
            print(f"::warning::{target.display_name}: {error}")
        print(
            f"{target.display_name}: baseline created and RSS completed ({version})"
        )
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
            target.display_name,
            old_version,
            version,
            diff_text,
            additions,
            deletions,
            timeout,
        )
    except RuntimeError as error:
        print(f"::warning::{target.display_name}: {error}")
        summary = fallback_summary(old_version, version, additions, deletions)

    description = rss_description(
        target.display_name, old_version, version, summary, additions, deletions
    )
    complete_rss_entry(target.rss_path, guid_value, description)
    asset_path.write_bytes(pdf_content)
    targets_state[target.key] = {
        "version": version,
        "pdf_url": pdf_url,
        "pdf_sha256": pdf_hash,
        "pdf_asset": target.pdf_asset,
        "updated_at": detected_at.isoformat(),
        "rss_description": description,
    }
    try:
        send_bark(
            session,
            bark_base_url,
            bark_token,
            f"{bark_title} - {target.display_name}",
            bark_body(old_version, version, additions, deletions, summary),
            pdf_url,
            bark_group,
            timeout,
        )
        print(f"{target.display_name}: Bark notification sent")
    except RuntimeError as error:
        print(f"::warning::{target.display_name}: {error}")
    print(f"{target.display_name}: updated {old_version} -> {version}")
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
    targets = configured_targets()
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    timeout = int(os.environ.get("MANUAL_REQUEST_TIMEOUT_SECONDS", "30"))
    page_fetcher = TavilyPageFetcher(
        timeout,
        required_env("TAVILY_API_KEY"),
        os.environ.get("TAVILY_EXTRACT_DEPTH", "advanced").strip(),
        os.environ.get("TAVILY_EXTRACT_FORMAT", "markdown").strip(),
    )
    pdf_fetcher = BrowserFetcher(timeout)
    errors: List[str] = []
    results = {}
    for target in targets:
        try:
            results[target.key] = process_target(
                session,
                target,
                state,
                args.state_dir,
                required_env("MANUAL_VERSION_TEXT_PREFIX"),
                os.environ.get("GEMINI_API_KEY", "").strip(),
                required_env("GEMINI_MODEL"),
                timeout,
                page_fetcher,
                pdf_fetcher,
                required_env("BARK_BASE_URL"),
                os.environ.get("BARK_TOKEN", "").strip(),
                required_env("MANUAL_BARK_TITLE"),
                required_env("MANUAL_BARK_GROUP"),
            )
        except Exception as error:
            message = f"{target.display_name}: {type(error).__name__}: {error}"
            print(f"::error::{message}")
            errors.append(message)
            results[target.key] = "failed"

    try:
        page_fetcher.close()
    except Exception as error:
        print(f"::warning::Tavily cleanup failed: {type(error).__name__}")
    try:
        pdf_fetcher.close()
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
