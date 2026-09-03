#!/usr/bin/env python3

import argparse
import json
import os
from pathlib import Path
from typing import Dict, Optional, Sequence
from urllib.parse import urljoin

import requests

try:
    from scripts.manual_version_monitor import derive_international_url
except ModuleNotFoundError:
    from manual_version_monitor import derive_international_url


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
PDF_FILENAME = "Owners_Manual.pdf"


def international_pdf_url(mainland_page_url: str) -> str:
    international_page_url = derive_international_url(mainland_page_url)
    return urljoin(international_page_url, PDF_FILENAME)


def probe_pdf(
    session: requests.Session,
    url: str,
    timeout: int,
) -> Dict[str, object]:
    response = session.get(
        url,
        headers={
            "Accept": "application/pdf,*/*;q=0.8",
            "Range": "bytes=0-511",
        },
        allow_redirects=True,
        stream=True,
        timeout=timeout,
    )
    try:
        first_chunk = next(response.iter_content(chunk_size=512), b"")
        status_ok = response.status_code in (200, 206)
        content_type = response.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        content_type_ok = media_type in {
            "application/pdf",
            "application/octet-stream",
            "binary/octet-stream",
        }
        signature_ok = first_chunk.startswith(b"%PDF-")
        body_preview = ""
        if not signature_ok:
            body_preview = first_chunk.decode("utf-8", errors="replace")[:300]
        return {
            "ok": status_ok and content_type_ok and signature_ok,
            "requested_url": url,
            "final_url": response.url,
            "status_code": response.status_code,
            "content_type": content_type,
            "content_type_accepted": content_type_ok,
            "content_length": response.headers.get("Content-Length", ""),
            "content_range": response.headers.get("Content-Range", ""),
            "server": response.headers.get("Server", ""),
            "reference_error": response.headers.get("X-Reference-Error", ""),
            "pdf_signature": signature_ok,
            "body_preview": body_preview,
            "environment_proxy_disabled": not session.trust_env,
        }
    finally:
        response.close()


def write_result(path: Path, result: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check whether an international owner-manual PDF is directly accessible."
    )
    parser.add_argument(
        "--mainland-page-url",
        default=os.environ.get("MODELY_MANUAL_URL", "").strip(),
        help="Mainland manual page URL whose .com counterpart will be tested.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=int(os.environ.get("MANUAL_REQUEST_TIMEOUT_SECONDS", "30")),
    )
    parser.add_argument("--result-file", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    result: Dict[str, object]
    try:
        if not args.mainland_page_url:
            raise RuntimeError(
                "MODELY_MANUAL_URL is empty and --mainland-page-url was not provided"
            )
        if args.timeout <= 0:
            raise ValueError("Timeout must be greater than zero")

        url = international_pdf_url(args.mainland_page_url)
        session = requests.Session()
        session.trust_env = False
        session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
        result = probe_pdf(session, url, args.timeout)
    except Exception as error:
        result = {
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
            "environment_proxy_disabled": True,
        }

    write_result(args.result_file, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("ok"):
        print("Direct PDF access succeeded and the response starts with %PDF-.")
        return 0

    print("::error::Direct PDF access failed; see the JSON result above.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
