from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://wikiwiki.jp/taiko-fumen"
LIST_URL = f"{BASE_URL}/?cmd=list"
SHUROKU = "\u53ce\u9332\u66f2"

COURSE_DIR = {
    "Easy": "\u304b\u3093\u305f\u3093",
    "Normal": "\u3075\u3064\u3046",
    "Hard": "\u3080\u305a\u304b\u3057\u3044",
    "Oni": "\u304a\u306b",
    "Edit": "\u304a\u306b",
}


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_text(url: str, timeout: int = 25) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 taiko-rating-preview/0.1"})
    return urlopen(request, timeout=timeout).read().decode("utf-8", "replace")


def fetch_page_list(cache_path: Path, refresh: bool, timeout: int) -> set[str]:
    cached = read_json(cache_path, {})
    if isinstance(cached, dict) and cached.get("pages") and not refresh:
        return set(str(page) for page in cached["pages"])

    html = fetch_text(LIST_URL, timeout=timeout)
    pages: set[str] = set()
    for match in re.finditer(r'<a [^>]*href="([^"]+)"[^>]*>.*?</a>', html, re.S):
        href = unescape(match.group(1))
        if "/taiko-fumen/" not in href:
            continue
        page = href.split("/taiko-fumen/", 1)[1].split("#", 1)[0].split("?", 1)[0]
        page = unquote(page)
        if page.startswith(f"{SHUROKU}/"):
            pages.add(page)

    payload = {
        "source": LIST_URL,
        "fetched_at": int(time.time()),
        "pages": sorted(pages),
    }
    write_json(cache_path, payload)
    return pages


def clean_wiki_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = re.sub(r"\s*[·]\s*.+$", "", text)
    text = re.sub(r"\s*[\(（]\s*\u88cf\s*[\)）]\s*$", "", text)
    text = re.sub(
        r"\s*[-\u2013\u2014]\s*(?:New|Old|Cover|Beena|Anomalous|Original|AC16)\s*"
        r"(?:Audio|Chart|Version)?(?:\s*/\s*(?:Audio|Chart))?\s*[-\u2013\u2014]?\s*$",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\s*[\(（](?:New|Old|Cover|Beena|Anomalous|Original|AC16)\s*(?:Audio|Chart|Version)?[\)）]\s*$",
        "",
        text,
        flags=re.I,
    )
    return text.replace("\u2661", "").strip()


def strip_song_name_suffix(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s*[\(（](?:\u304a\u306b|\u304a\u306b\u88cf|\u3080\u305a\u304b\u3057\u3044|\u3075\u3064\u3046|\u304b\u3093\u305f\u3093)[\)）]\s*$", "", text)
    return text.strip()


def dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def candidate_names(chart: dict[str, Any]) -> list[str]:
    fumen = chart.get("fumen") if isinstance(chart.get("fumen"), dict) else {}
    raw_values = [
        fumen.get("list_title"),
        strip_song_name_suffix(fumen.get("song_name")),
        chart.get("title"),
        *(chart.get("aliases") or []),
    ]
    names: list[str] = []
    for value in raw_values:
        if not value:
            continue
        raw = str(value).strip()
        cleaned = clean_wiki_name(raw)
        names.extend([raw, cleaned])
    return dedupe(names)


def wiki_pages_for(course: str, name: str) -> list[str]:
    course_dir = COURSE_DIR.get(course)
    if not course_dir or not name:
        return []
    base_page = f"{SHUROKU}/{course_dir}/{name}"
    if course == "Edit":
        return [f"{base_page}(\u88cf\u8b5c\u9762)", base_page]
    return [base_page]


def source_page_url(page: str) -> str:
    parts = [quote(part, safe="") for part in page.split("/")]
    return f"{BASE_URL}/{'/'.join(parts)}"


def source_view_url(page: str) -> str:
    return f"{BASE_URL}/::cmd/source?page={quote(page, safe='')}"


def cdn_ref_url(page: str, filename: str) -> str:
    page_parts = [quote(part, safe="") for part in page.split("/")]
    cleaned = filename.strip().lstrip("./")
    return f"https://cdn.wikiwiki.jp/to/w/taiko-fumen/{'/'.join(page_parts)}/::ref/{quote(cleaned, safe='')}"


def match_chart_to_page(chart: dict[str, Any], pages: set[str]) -> dict[str, Any] | None:
    course = str(chart.get("course") or "")
    for name in candidate_names(chart):
        for page in wiki_pages_for(course, name):
            if page in pages:
                return {
                    "status": "matched",
                    "page": page,
                    "source_page": source_page_url(page),
                    "matched_name": name,
                    "images": [],
                }
    return None


def extract_source_text(html: str) -> str:
    match = re.search(r'<pre[^>]*id="source"[^>]*>\s*<code[^>]*>(.*?)</code>\s*</pre>', html, re.S)
    if not match:
        match = re.search(r'<pre[^>]*class="[^"]*\bwiki-source\b[^"]*"[^>]*>\s*<code[^>]*>(.*?)</code>\s*</pre>', html, re.S)
    return unescape(match.group(1)) if match else ""


def source_score_windows(source: str) -> list[str]:
    lines = source.splitlines()
    starts: list[int] = []
    for index, line in enumerate(lines):
        if re.match(r"^\*+\s*(?:譜面|画像|プレイ動画)", line):
            starts.append(index)
        elif "譜面" in line and re.match(r"^\*+", line):
            starts.append(index)
    windows: list[str] = []
    for start in starts:
        current_level = len(re.match(r"^\*+", lines[start]).group(0)) if re.match(r"^\*+", lines[start]) else 1
        end = len(lines)
        for index in range(start + 1, len(lines)):
            heading = re.match(r"^(\*+)", lines[index])
            if heading and len(heading.group(1)) <= current_level:
                end = index
                break
        windows.append("\n".join(lines[start:end]))
    return windows or [source]


def split_ref_args(value: str) -> list[str]:
    args: list[str] = []
    current: list[str] = []
    quote_char = ""
    escape = False
    for char in value:
        if escape:
            current.append(char)
            escape = False
            continue
        if char == "\\":
            current.append(char)
            escape = True
            continue
        if quote_char:
            current.append(char)
            if char == quote_char:
                quote_char = ""
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote_char = char
            continue
        if char == ",":
            args.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    args.append("".join(current).strip())
    return args


def clean_ref_filename(value: str) -> str:
    filename = value.strip().strip("'\"")
    filename = filename.replace("&amp;", "&")
    filename = unquote(filename)
    if "#" in filename:
        filename = filename.split("#", 1)[0]
    if "?" in filename:
        filename = filename.split("?", 1)[0]
    return filename.strip()


def looks_like_preview_filename(filename: str) -> bool:
    if not re.search(r"\.(?:png|jpe?g|webp|gif)$", filename, re.I):
        return False
    lowered = filename.casefold()
    blocked = [
        "header",
        "qr",
        "qrcode",
        "コード",
        "jacket",
        "banner",
        "logo",
        "icon",
        "sound",
        "audio",
        "動画",
        "youtube",
    ]
    return not any(token in lowered for token in blocked)


def ref_candidates_from_source(source: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    windows = source_score_windows(source)
    for pass_index, window in enumerate(windows + ([] if windows == [source] else [source])):
        for match in re.finditer(r"(?:attachref|ref)\(([^)\r\n]+)\)", window, re.I):
            args = split_ref_args(match.group(1))
            if not args:
                continue
            filename = clean_ref_filename(args[0])
            if not filename or filename.lower().startswith(("http://", "https://")):
                continue
            if not looks_like_preview_filename(filename):
                continue
            context = "score_section" if pass_index < len(windows) else "full_source"
            candidates.append((filename, context))
        if candidates:
            break
    return candidates


def parse_source_images(html: str, page: str, source_page: str, max_images: int) -> list[dict[str, Any]]:
    source = extract_source_text(html)
    if not source:
        return []
    images: list[dict[str, Any]] = []
    seen: set[str] = set()
    for filename, context in ref_candidates_from_source(source):
        if filename in seen:
            continue
        seen.add(filename)
        images.append(
            {
                "url": cdn_ref_url(page, filename),
                "alt": filename,
                "source_page": source_page,
                "source": "wiki_source_attachref",
                "source_context": context,
            }
        )
        if len(images) >= max_images:
            break
    return images


def parse_images(html: str, source_page: str, max_images: int) -> list[dict[str, Any]]:
    images: list[dict[str, Any]] = []
    img_re = re.compile(r'<img [^>]*src="(https://cdn\.wikiwiki\.jp/to/w/taiko-fumen/[^"<>]+)"[^>]*>', re.S)
    for tag_match in img_re.finditer(html):
        tag = tag_match.group(0)
        src = unescape(tag_match.group(1)).replace("&amp;", "&")
        if "/::ref/" not in src or "%3AHeader" in src or ":Header" in src:
            continue
        alt = unescape((re.search(r'alt="([^"]*)"', tag) or [None, ""])[1])
        width = int((re.search(r'width="(\d+)"', tag) or [None, "0"])[1])
        height = int((re.search(r'height="(\d+)"', tag) or [None, "0"])[1])
        if re.search(r"(?:qr|QR|\u30b3\u30fc\u30c9)", alt) or re.search(r"(?:qr|QR|%E3%82%B3%E3%83%BC%E3%83%89)", src):
            continue
        if width < 500 or height < 240:
            continue
        if not re.search(r"\.(?:png|jpe?g|webp|gif)(?:\?|$)", src, re.I):
            continue
        images.append(
            {
                "url": src,
                "width": width,
                "height": height,
                "alt": alt,
                "source_page": source_page,
                "source": "wiki_rendered_img",
            }
        )
    return images[:max_images]


class RateLimiter:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = max(0.0, delay_seconds)
        self.lock = threading.Lock()
        self.last_at = 0.0

    def wait(self) -> None:
        if self.delay_seconds <= 0:
            return
        with self.lock:
            now = time.monotonic()
            wait_for = self.last_at + self.delay_seconds - now
            if wait_for > 0:
                time.sleep(wait_for)
            self.last_at = time.monotonic()


def fetch_preview(
    record: dict[str, Any],
    limiter: RateLimiter,
    timeout: int,
    max_images: int,
    fetch_mode: str,
) -> tuple[str, dict[str, Any]]:
    record_id = str(record["id"])
    source_page = str(record["source_page"])
    page = str(record["page"])
    url = source_view_url(page) if fetch_mode == "source" else source_page
    limiter.wait()
    try:
        html = fetch_text(url, timeout=timeout)
        if fetch_mode == "source":
            images = parse_source_images(html, page, source_page, max_images=max_images)
        else:
            images = parse_images(html, source_page, max_images=max_images)
        payload = {key: value for key, value in record.items() if key != "error"}
        return record_id, {
            **payload,
            "status": "ok" if images else "no_images",
            "images": images,
            "fetch_mode": fetch_mode,
        }
    except HTTPError as exc:
        return record_id, {
            **record,
            "status": "error",
            "error": f"HTTP {exc.code}",
            "images": [],
            "fetch_mode": fetch_mode,
        }
    except (OSError, URLError, TimeoutError) as exc:
        return record_id, {
            **record,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "images": [],
            "fetch_mode": fetch_mode,
        }


def build_summary(charts: list[Any], pages: set[str], matched: dict[str, dict[str, Any]], fetched: int) -> dict[str, Any]:
    summary = {
        "chart_rows": len(charts),
        "wiki_pages": len(pages),
        "matched_rows": len(matched),
        "fetched_this_run": fetched,
        "with_images": sum(1 for item in matched.values() if item.get("images")),
        "without_images": sum(1 for item in matched.values() if item.get("status") == "no_images"),
        "errors": sum(1 for item in matched.values() if item.get("status") == "error"),
        "status_counts": {},
    }
    for item in matched.values():
        status = str(item.get("status") or "matched")
        summary["status_counts"][status] = summary["status_counts"].get(status, 0) + 1
    return summary


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Build WikiWiki chart preview image metadata.")
    parser.add_argument("--chart-data", type=Path, default=PROJECT_ROOT / "data" / "chart_data.json")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "wiki_preview_images.json")
    parser.add_argument("--summary", type=Path, default=PROJECT_ROOT / "data" / "wiki_preview_images_summary.json")
    parser.add_argument("--page-list-cache", type=Path, default=PROJECT_ROOT / "data" / "wiki_page_list.json")
    parser.add_argument("--refresh-page-list", action="store_true")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.5, help="Global delay between WikiWiki page requests.")
    parser.add_argument("--timeout", type=int, default=25)
    parser.add_argument("--max-images", type=int, default=2)
    parser.add_argument("--fetch-mode", choices=["source", "rendered"], default="source")
    parser.add_argument("--limit-pages", type=int, default=0, help="Fetch at most this many uncached pages this run.")
    parser.add_argument("--force", action="store_true", help="Refetch pages even if cached.")
    parser.add_argument("--skip-errors", action="store_true", help="Do not retry pages that previously returned an error.")
    parser.add_argument("--stop-on-429", action="store_true", help="Stop the run as soon as WikiWiki starts rate-limiting.")
    parser.add_argument("--checkpoint-every", type=int, default=50, help="Write output and summary every N fetched pages.")
    parser.add_argument("--id-contains", action="append", default=[], help="Only fetch matched chart ids containing this text.")
    args = parser.parse_args()

    charts = read_json(args.chart_data, [])
    pages = fetch_page_list(args.page_list_cache, refresh=args.refresh_page_list, timeout=args.timeout)
    existing = read_json(args.output, {})
    if not isinstance(existing, dict):
        existing = {}

    matched: dict[str, dict[str, Any]] = {}
    for chart in charts:
        record_id = str(chart.get("id"))
        if not record_id:
            continue
        match = match_chart_to_page(chart, pages)
        if match:
            cached = existing.get(record_id, {}) if isinstance(existing.get(record_id), dict) else {}
            if cached.get("page") and cached.get("page") != match["page"]:
                cached = {}
            matched[record_id] = {**match, **cached, **{key: match[key] for key in ["page", "source_page", "matched_name"]}}

    pending = [
        {"id": record_id, **record}
        for record_id, record in matched.items()
        if args.force or record.get("status") not in {"ok", "no_images"}
    ]
    if args.skip_errors and not args.force:
        pending = [record for record in pending if record.get("status") != "error"]
    for needle in args.id_contains:
        pending = [record for record in pending if needle in record["id"]]
    if args.limit_pages > 0:
        pending = pending[: args.limit_pages]

    limiter = RateLimiter(args.delay)
    fetched = 0

    def checkpoint() -> None:
        summary = build_summary(charts, pages, matched, fetched)
        write_json(args.output, matched)
        write_json(args.summary, summary)

    if args.stop_on_429 and args.workers <= 1:
        for record in pending:
            record_id, payload = fetch_preview(record, limiter, args.timeout, args.max_images, args.fetch_mode)
            matched[record_id] = payload
            fetched += 1
            if args.checkpoint_every > 0 and fetched % args.checkpoint_every == 0:
                checkpoint()
            if fetched % 100 == 0:
                print(f"fetched {fetched}/{len(pending)}", flush=True)
            if payload.get("error") == "HTTP 429":
                print(f"stopped after HTTP 429 at {fetched}/{len(pending)}", flush=True)
                break
    else:
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [
                pool.submit(fetch_preview, record, limiter, args.timeout, args.max_images, args.fetch_mode)
                for record in pending
            ]
            for future in as_completed(futures):
                record_id, payload = future.result()
                matched[record_id] = payload
                fetched += 1
                if args.checkpoint_every > 0 and fetched % args.checkpoint_every == 0:
                    checkpoint()
                if fetched % 100 == 0:
                    print(f"fetched {fetched}/{len(pending)}", flush=True)

    summary = build_summary(charts, pages, matched, fetched)
    write_json(args.output, matched)
    write_json(args.summary, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
