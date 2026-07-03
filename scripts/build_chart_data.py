from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any


COURSE_LEVEL = {
    "easy": 1,
    "normal": 2,
    "hard": 3,
    "oni": 4,
    "edit": 5,
}

COURSE_LABEL = {
    "Easy": "梅",
    "Normal": "竹",
    "Hard": "困难",
    "Oni": "魔王",
    "Edit": "里魔王",
}

DEFAULT_INCLUDED_COURSES = {"hard", "oni", "edit"}

FORCE_NORMAL_TITLES = [
    "ダーク・エクス・マキナ",
    "Dark Ex Machina",
    "幽玄ノ乱",
    "Yuugen no Ran",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def as_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def clean_course(course: str) -> str:
    name = str(course or "").strip()
    return name[:1].upper() + name[1:].lower()


def normalize_title(title: str) -> str:
    value = unicodedata.normalize("NFKC", str(title or "")).casefold()
    value = value.replace("♡", "")
    value = value.replace("～", "~")
    value = value.replace("・", "")
    value = value.replace("ノ", "の")
    value = re.sub(r"\(裏\)|（裏）|\bura\b|\bedit\b", "", value)
    value = re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", value)
    return value


def title_blob(row: dict[str, str]) -> str:
    return " ".join(
        str(row.get(key, ""))
        for key in ["title", "title_ja", "title_zh", "ese_title", "ese_title_ja", "ese_title_zh"]
    )


def is_forced_normal(row: dict[str, str]) -> bool:
    if clean_course(row.get("course", "")).casefold() != "normal":
        return False
    blob = title_blob(row)
    normalized_blob = normalize_title(blob)
    return any(normalize_title(title) in normalized_blob for title in FORCE_NORMAL_TITLES)


def is_target_course(row: dict[str, str]) -> bool:
    course = clean_course(row.get("course", "")).casefold()
    return course in DEFAULT_INCLUDED_COURSES or is_forced_normal(row)


def excel_stats(row: dict[str, str]) -> dict[str, Any]:
    return {
        "const": as_float(row.get("const")),
        "combo": as_int(row.get("combo")),
        "features": {
            "complex": as_float(row.get("complex")),
            "avg_density": as_float(row.get("avg_density")),
            "peak_density": as_float(row.get("peak_density")),
            "note_type": as_float(row.get("note_type")),
            "bpm_change": as_float(row.get("bpm_change")),
            "hs_change": as_float(row.get("hs_change")),
            "rhythm": as_float(row.get("rhythm")),
        },
        "roll_time": row.get("roll_time") or None,
        "balloon_num": as_int(row.get("balloon_num"), 0),
    }


def build_excel_by_course(strict_rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in strict_rows:
        path = str(row.get("ese_path", ""))
        course = clean_course(row.get("ese_course", ""))
        if not path or not course:
            continue
        out[(path, course.casefold())] = row
    return out


def maybe_anomaly_dark(row: dict[str, str]) -> bool:
    blob = title_blob(row)
    return "anomalous" in blob.casefold() or "anomaly" in str(row.get("path", "")).casefold()


def should_keep_forced_duplicate(row: dict[str, str]) -> bool:
    # The ESE tree contains both the normal Dark Ex Machina and an Anomalous Audio
    # variant with the same Japanese title. The special case means the normal
    # song's竹 chart, not the Anomalous Audio variant.
    if "darkexmachina" in normalize_title(title_blob(row)):
        return not maybe_anomaly_dark(row)
    return True


def make_record(row: dict[str, str], excel_row: dict[str, str] | None) -> dict[str, Any]:
    course = clean_course(row.get("course", ""))
    course_level = COURSE_LEVEL.get(course.casefold())
    source = "excel" if excel_row is not None else "encoder_pending"
    stats = excel_stats(excel_row) if excel_row is not None else {
        "const": None,
        "combo": as_int(row.get("note_count")),
        "features": {
            "complex": None,
            "avg_density": None,
            "peak_density": None,
            "note_type": None,
            "bpm_change": None,
            "hs_change": None,
            "rhythm": None,
        },
        "roll_time": None,
        "balloon_num": None,
    }
    title = row.get("title_zh") or row.get("title_ja") or row.get("title") or ""
    aliases = [
        value
        for value in [row.get("title"), row.get("title_ja"), row.get("title_zh")]
        if value and value != title
    ]
    return {
        "id": f"ese:{row.get('path')}::{course}",
        "title": title,
        "aliases": aliases,
        "title_normalized": normalize_title(title),
        "course": course,
        "course_label": COURSE_LABEL.get(course, course),
        "level": as_int(row.get("level")),
        "score_level": course_level,
        "is_ura": str(row.get("is_ura", "")).lower() == "true",
        "has_branch": str(row.get("has_branch", "")).lower() == "true",
        "ese": {
            "path": row.get("path"),
            "category": row.get("category"),
            "note_count": as_int(row.get("note_count")),
            "roll_start_count": as_int(row.get("roll_start_count")),
            "balloon_declared": row.get("balloon_declared") or None,
        },
        **stats,
        "source": source,
        "needs_encoder": excel_row is None,
        "force_included": is_forced_normal(row),
        "review": {
            "matched_combo_delta": as_int(excel_row.get("combo_delta")) if excel_row else None,
            "matched_combo_delta_threshold": as_int(excel_row.get("combo_delta_threshold")) if excel_row else None,
        },
    }


def build_chart_data(ese_rows: list[dict[str, str]], excel_by_course: dict[tuple[str, str], dict[str, str]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in ese_rows:
        course = clean_course(row.get("course", ""))
        row = {**row, "course": course}
        if not is_target_course(row):
            continue
        if is_forced_normal(row) and not should_keep_forced_duplicate(row):
            continue
        key = (str(row.get("path", "")), course.casefold())
        if key in seen:
            continue
        seen.add(key)
        excel_row = excel_by_course.get(key)
        records.append(make_record(row, excel_row))

    records.sort(key=lambda item: (item["course"] != "Edit", item["course"] != "Oni", item["course"] != "Hard", item["title"]))
    return records


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_course = Counter(row["course"] for row in records)
    by_source = Counter(row["source"] for row in records)
    forced = [row for row in records if row["force_included"]]
    return {
        "total": len(records),
        "by_course": dict(sorted(by_course.items())),
        "by_source": dict(sorted(by_source.items())),
        "needs_encoder": sum(1 for row in records if row["needs_encoder"]),
        "force_included": [
            {
                "title": row["title"],
                "course": row["course"],
                "level": row["level"],
                "source": row["source"],
                "path": row["ese"]["path"],
            }
            for row in forced
        ],
    }


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Build static Taiko chart data for the rating frontend.")
    parser.add_argument("--ese-courses", type=Path, default=Path("../taiko_encoder_out/ese_courses.csv"))
    parser.add_argument("--strict-matched", type=Path, default=Path("../taiko_encoder_out/strict_matched_dataset.csv"))
    parser.add_argument("--output", type=Path, default=Path("data/chart_data.json"))
    parser.add_argument("--summary", type=Path, default=Path("data/chart_data_summary.json"))
    args = parser.parse_args()

    ese_rows = read_csv(args.ese_courses)
    strict_rows = read_csv(args.strict_matched)
    excel_by_course = build_excel_by_course(strict_rows)
    records = build_chart_data(ese_rows, excel_by_course)
    summary = summarize(records)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
