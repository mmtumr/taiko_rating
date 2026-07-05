from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from build_chart_data import clean_course, is_target_course, normalize_title


BASE_URL = "https://fumen-database.com"
DIFFICULTY_URL = f"{BASE_URL}/difficulty"
COURSE_BY_FUMEN_ID = {
    "4": "Oni",
    "5": "Edit",
}

# ESE's note_count for these charts includes branch/hidden-count artifacts, while
# fumen-database has the official max combo. Keep the name/course match but record
# that the combo guard was bypassed.
ALLOW_COMBO_MISMATCH_IDS = {
    r"ese:ESE-master\ese\09 Namco Original\Ka\Ka.tja::Edit",
    r"ese:ESE-master\ese\09 Namco Original\Ka\Ka.tja::Oni",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_text(url: str, timeout: int, retries: int) -> tuple[str, str]:
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request = Request(url, headers={"User-Agent": "Mozilla/5.0 taiko-rating/0.1"})
            response = urlopen(request, timeout=timeout)
            return response.geturl(), response.read().decode("utf-8", "replace")
        except (OSError, URLError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def strip_tags(value: str | None) -> str | None:
    if value is None:
        return None
    text = re.sub(r"<[^>]+>", "", value)
    return unescape(text).strip()


def first_match(pattern: str, html: str) -> str | None:
    match = re.search(pattern, html, re.S)
    return unescape(match.group(1).strip()) if match else None


def number(value: Any) -> float | None:
    if value is None:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(match.group(0)) if match else None


def integer(value: Any) -> int | None:
    numeric = number(value)
    return int(round(numeric)) if numeric is not None else None


def clean_roll_time(value: str | None) -> str | None:
    if not value:
        return None
    return strip_tags(value)


def roll_time_seconds(value: str | None) -> float | None:
    return number(value)


def rhythm_from_fumen(note_type: float | None, bpm_change: float | None) -> float | None:
    if note_type is None or bpm_change is None:
        return None
    return round((0.9 * note_type**10 + 0.1 * bpm_change**10) ** 0.1, 12)


def parse_difficulty_page(html: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    link_re = re.compile(r'href=["\'](/song/(\d+)-(\d+)/?)["\'][^>]*>(.*?)</a>', re.S)
    for match in link_re.finditer(html):
        href, song_id, course_id, raw_title = match.groups()
        course = COURSE_BY_FUMEN_ID.get(course_id)
        title = strip_tags(raw_title)
        if not course or not title:
            continue
        url = f"{BASE_URL}{href}"
        if url in seen:
            continue
        seen.add(url)
        entries.append(
            {
                "url": url,
                "song_id": song_id,
                "course_id": course_id,
                "course": course,
                "title": title,
                "title_normalized": normalize_title(title),
            }
        )
    return entries


def text_after_image(html: str, image_name: str) -> str | None:
    pattern = rf'{re.escape(image_name)}.*?<p>(.*?)</p>'
    return strip_tags(first_match(pattern, html))


def parse_song_page(url: str, final_url: str, html: str) -> dict[str, Any]:
    features = {
        "complex": number(first_match(r"radar_compound:\s*'([^']+)'", html)),
        "avg_density": number(first_match(r"radar_density_ave:\s*'([^']+)'", html)),
        "peak_density": number(first_match(r"radar_density_inst:\s*'([^']+)'", html)),
        "note_type": number(first_match(r"radar_division:\s*'([^']+)'", html)),
        "bpm_change": number(first_match(r"radar_change_bpm:\s*'([^']+)'", html)),
        "hs_change": number(first_match(r"radar_change_hs:\s*'([^']+)'", html)),
    }
    features["rhythm"] = rhythm_from_fumen(features["note_type"], features["bpm_change"])

    roll_time = clean_roll_time(text_after_image(html, "title_rollTime.png"))
    stats = {
        "const": number(first_match(r'id=["\']score_const_origin["\'][^>]*>([^<]+)', html)),
        "combo": integer(text_after_image(html, "title_combo.png")),
        "features": features,
        "roll_time": roll_time,
        "roll_time_seconds": roll_time_seconds(roll_time),
        "balloon_num": integer(text_after_image(html, "title_balloonNum.png")) or 0,
        "fumen": {
            "url": url,
            "final_url": final_url,
            "title": strip_tags(first_match(r"<title>\s*([^<]+)</title>", html)),
            "song_name": first_match(r'song_name:\s*"([^"]+)"', html),
            "status": "ok",
        },
    }

    required = [stats["const"], stats["combo"], *features.values()]
    if any(value is None for value in required):
        missing = [
            key
            for key, value in {
                "const": stats["const"],
                "combo": stats["combo"],
                **features,
            }.items()
            if value is None
        ]
        raise ValueError(f"missing fields: {', '.join(missing)}")
    return stats


def row_record_id(row: dict[str, str]) -> str:
    return f"ese:{row.get('path')}::{clean_course(row.get('course', ''))}"


def row_key(row: dict[str, str]) -> tuple[str, str]:
    return str(row.get("path", "")), clean_course(row.get("course", "")).casefold()


def strict_key(row: dict[str, str]) -> tuple[str, str]:
    return str(row.get("ese_path", "")), clean_course(row.get("ese_course", "")).casefold()


def row_name_keys(row: dict[str, str]) -> set[str]:
    names = [
        row.get("title"),
        row.get("title_ja"),
        row.get("title_zh"),
        row.get("title_ko"),
        *(row.get("normalized_keys") or "").split("|"),
    ]
    keys: set[str] = set()
    suffix_re = re.compile(
        r"\s*[-–—]\s*(?:new|old|cover|beena)\s*(?:audio|chart|version)?(?:\s*/\s*(?:audio|chart))?\s*[-–—]?\s*$",
        re.I,
    )
    normalized_suffix_re = re.compile(
        r"(?:newaudio|oldaudio|coveraudiochart|coveraudio|newchart|oldchart|newversion|oldversion|beenaversion)$",
        re.I,
    )

    for name in names:
        if not name:
            continue
        raw = str(name).strip()
        variants = {raw, suffix_re.sub("", raw)}
        for variant in variants:
            key = normalize_title(variant)
            if key:
                keys.add(key)
                stripped_key = normalized_suffix_re.sub("", key)
                if stripped_key:
                    keys.add(stripped_key)
    return keys


def combo_threshold(combo: int | None) -> int:
    if combo is None:
        return 0
    return max(50, int(round(combo * 0.05)))


def build_candidates(
    ese_rows: list[dict[str, str]],
    strict_rows: list[dict[str, str]],
    difficulty_entries: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    excel_keys = {strict_key(row) for row in strict_rows}
    by_name_course: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in difficulty_entries:
        key = (entry["title_normalized"], str(entry["course"]))
        by_name_course.setdefault(key, []).append(entry)

    candidates: dict[str, list[dict[str, Any]]] = {}
    target_rows = 0
    for row in ese_rows:
        row = {**row, "course": clean_course(row.get("course", ""))}
        if not is_target_course(row):
            continue
        if row["course"] not in {"Oni", "Edit"}:
            continue
        if row_key(row) in excel_keys:
            continue
        target_rows += 1

        found: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        for name_key in row_name_keys(row):
            for entry in by_name_course.get((name_key, row["course"]), []):
                if entry["url"] in seen_urls:
                    continue
                seen_urls.add(entry["url"])
                found.append(entry)
        if found:
            candidates[row_record_id(row)] = found

    summary = {
        "target_rows_without_xlsx": target_rows,
        "rows_with_name_candidate": len(candidates),
        "unique_candidate_urls": len({entry["url"] for entries in candidates.values() for entry in entries}),
    }
    return candidates, summary


def fetch_pages(
    urls: list[str],
    cache: dict[str, Any],
    workers: int,
    timeout: int,
    retries: int,
    force: bool,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    pending = [url for url in urls if force or cache.get(url, {}).get("status") != "ok"]

    def fetch_one(url: str) -> tuple[str, dict[str, Any]]:
        final_url, html = fetch_text(url, timeout=timeout, retries=retries)
        stats = parse_song_page(url, final_url, html)
        return url, {"status": "ok", "stats": stats}

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(fetch_one, url): url for url in pending}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    fetched_url, payload = future.result()
                    cache[fetched_url] = payload
                except Exception as exc:  # noqa: BLE001 - keep scraping the rest.
                    message = f"{type(exc).__name__}: {exc}"
                    cache[url] = {"status": "error", "error": message}
                    errors.append({"url": url, "error": message})
    return cache, errors


def match_rows_to_stats(
    ese_rows: list[dict[str, str]],
    candidates: dict[str, list[dict[str, Any]]],
    page_cache: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    rows_by_id = {row_record_id(row): {**row, "course": clean_course(row.get("course", ""))} for row in ese_rows}
    output: dict[str, Any] = {}
    rejected_combo = 0
    missing_page_stats = 0
    ambiguous = 0
    relaxed_combo = 0

    for record_id, entries in candidates.items():
        row = rows_by_id.get(record_id)
        if row is None:
            continue
        note_count = integer(row.get("note_count"))
        valid: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
        for entry in entries:
            payload = page_cache.get(entry["url"], {})
            stats = payload.get("stats") if payload.get("status") == "ok" else None
            if not isinstance(stats, dict):
                missing_page_stats += 1
                continue
            combo = integer(stats.get("combo"))
            threshold = combo_threshold(combo)
            delta = abs((combo or 0) - (note_count or 0)) if combo is not None and note_count is not None else 0
            is_combo_match = combo is None or note_count is None or delta <= threshold
            is_known_mismatch = record_id in ALLOW_COMBO_MISMATCH_IDS
            is_relaxed_single_candidate = not is_combo_match and len(entries) == 1
            if not is_combo_match and not is_known_mismatch and not is_relaxed_single_candidate:
                rejected_combo += 1
                continue
            valid.append((delta, threshold, is_known_mismatch, is_relaxed_single_candidate, entry, stats))

        if not valid:
            continue
        valid.sort(key=lambda item: (item[2], item[3], item[0], item[4]["url"]))
        if len(valid) > 1 and valid[0][0] == valid[1][0]:
            ambiguous += 1
            continue

        delta, threshold, is_known_mismatch, is_relaxed_single_candidate, entry, stats = valid[0]
        combo_match_mode = "strict"
        if is_known_mismatch:
            combo_match_mode = "known_mismatch"
        elif is_relaxed_single_candidate:
            combo_match_mode = "relaxed_single_candidate"
            relaxed_combo += 1
        matched = json.loads(json.dumps(stats, ensure_ascii=False))
        matched["fumen"] = {
            **matched.get("fumen", {}),
            "source_url": entry["url"],
            "song_id": entry["song_id"],
            "course_id": entry["course_id"],
            "course": entry["course"],
            "list_title": entry["title"],
            "match": {
                "combo_delta": delta,
                "combo_delta_threshold": threshold,
                "ese_note_count": note_count,
                "combo_match_mode": combo_match_mode,
                "combo_mismatch_allowed": is_known_mismatch or is_relaxed_single_candidate,
            },
        }
        output[record_id] = matched

    summary = {
        "generated": len(output),
        "rejected_combo": rejected_combo,
        "missing_page_stats": missing_page_stats,
        "ambiguous": ambiguous,
        "relaxed_combo": relaxed_combo,
    }
    return output, summary


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Scrape fumen-database chart stats for ESE charts missing xlsx data.")
    parser.add_argument("--ese-courses", type=Path, default=Path("../taiko_encoder_out/ese_courses.csv"))
    parser.add_argument("--strict-matched", type=Path, default=Path("../taiko_encoder_out/strict_matched_dataset.csv"))
    parser.add_argument("--output", type=Path, default=Path("data/fumen_chart_stats.json"))
    parser.add_argument("--summary", type=Path, default=Path("data/fumen_chart_stats_summary.json"))
    parser.add_argument("--page-cache", type=Path, default=Path("data/fumen_page_cache.json"))
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--timeout", type=int, default=20)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--force", action="store_true", help="Refetch pages even when the page cache has ok stats.")
    parser.add_argument("--limit-urls", type=int, default=0, help="Optional fetch limit for quick validation.")
    args = parser.parse_args()

    _, difficulty_html = fetch_text(DIFFICULTY_URL, timeout=args.timeout, retries=args.retries)
    difficulty_entries = parse_difficulty_page(difficulty_html)
    ese_rows = read_csv(args.ese_courses)
    strict_rows = read_csv(args.strict_matched)
    candidates, candidate_summary = build_candidates(ese_rows, strict_rows, difficulty_entries)

    urls = sorted({entry["url"] for entries in candidates.values() for entry in entries})
    if args.limit_urls > 0:
        allowed = set(urls[: args.limit_urls])
        candidates = {
            record_id: [entry for entry in entries if entry["url"] in allowed]
            for record_id, entries in candidates.items()
        }
        candidates = {record_id: entries for record_id, entries in candidates.items() if entries}
        urls = sorted(allowed)

    page_cache = read_json(args.page_cache, {})
    page_cache, fetch_errors = fetch_pages(
        urls,
        page_cache,
        workers=args.workers,
        timeout=args.timeout,
        retries=args.retries,
        force=args.force,
    )
    output, match_summary = match_rows_to_stats(ese_rows, candidates, page_cache)

    summary = {
        "difficulty_url": DIFFICULTY_URL,
        "difficulty_entries": len(difficulty_entries),
        **candidate_summary,
        "candidate_urls_used": len(urls),
        "page_cache_ok": sum(1 for item in page_cache.values() if item.get("status") == "ok"),
        "page_cache_error": sum(1 for item in page_cache.values() if item.get("status") == "error"),
        "fetch_errors": len(fetch_errors),
        **match_summary,
        "error_samples": fetch_errors[:10],
    }

    write_json(args.page_cache, page_cache)
    write_json(args.output, output)
    write_json(args.summary, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
