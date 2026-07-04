from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DIFFUSION_ROOT = WORKSPACE_ROOT / "taiko-diffusion"

if str(DIFFUSION_ROOT) not in sys.path:
    sys.path.insert(0, str(DIFFUSION_ROOT))

import torch  # noqa: E402

from taiko_diffusion.config import load_config  # noqa: E402
from taiko_diffusion.data.grid import chart_to_grid  # noqa: E402
from taiko_diffusion.data.tja import BranchUnsupportedError, TjaParseError, parse_tja_course, read_raw_courses  # noqa: E402
from taiko_diffusion.eval_encoder import build_model, inverse_targets  # noqa: E402


FEATURE_KEYS = [
    "complex",
    "avg_density",
    "peak_density",
    "note_type",
    "bpm_change",
    "hs_change",
    "rhythm",
]


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_workspace_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return WORKSPACE_ROOT / path


def is_missing_chart(row: dict[str, Any]) -> bool:
    return bool(row.get("needs_encoder")) or row.get("source") != "excel"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def numeric(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(number):
        return default
    return number


def round_or_int(value: Any, default: int = 0) -> int:
    return int(round(max(0.0, numeric(value, float(default)))))


def format_roll_time(seconds: float) -> str:
    return f"{max(0.0, seconds):.3f}秒"


def parse_ambiguous_chart(path: Path, course: str, level: Any, discard_branch: bool) -> tuple[Any, str]:
    raw_courses = [
        raw
        for raw in read_raw_courses(path)
        if str(raw.meta.get("COURSE", "")).casefold() == course.casefold()
    ]
    if not raw_courses:
        raise TjaParseError(f"Course {course!r} not found in ambiguous fallback: {path}")

    level_text = str(level or "").strip()
    level_matches = [raw for raw in raw_courses if str(raw.meta.get("LEVEL", "")).strip() == level_text]
    selected = (level_matches or raw_courses)[0]

    lines: list[str] = []
    for key, value in selected.meta.items():
        if key != "COURSE":
            lines.append(f"{key}:{value}")
    lines.append(f"COURSE:{selected.meta.get('COURSE', course)}")
    lines.append("#START")
    lines.extend(selected.body)
    lines.append("#END")

    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".tja", encoding="utf-8", delete=False) as temp:
            temp_name = temp.name
            temp.write("\n".join(lines))
            temp.write("\n")
        try:
            parsed = parse_tja_course(Path(temp_name), course, discard_branch=discard_branch)
            mode = "level_match"
        except BranchUnsupportedError:
            parsed = parse_tja_course(Path(temp_name), course, discard_branch=False)
            mode = "level_match_branch"
        parsed.path = str(path)
        return parsed, mode
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def parse_chart(row: dict[str, Any], discard_branch: bool) -> tuple[Any, Any, str]:
    ese = row.get("ese") or {}
    path_text = str(ese.get("path") or "")
    course = str(row.get("course") or "")
    if not path_text or not course:
        raise ValueError("chart row is missing ese.path or course")
    path = resolve_workspace_path(path_text)
    try:
        parsed = parse_tja_course(path, course, discard_branch=discard_branch)
        mode = "discard_branch" if discard_branch else "raw_branch"
    except BranchUnsupportedError:
        parsed = parse_tja_course(path, course, discard_branch=False)
        mode = "raw_branch"
    except TjaParseError as exc:
        if "ambiguous" not in str(exc).casefold():
            raise
        parsed, mode = parse_ambiguous_chart(path, course, row.get("level"), discard_branch)
    return parsed, path, mode


def prediction_to_stats(
    row: dict[str, Any],
    raw_prediction: np.ndarray,
    label_names: list[str],
    parsed: Any,
    grid: Any,
    parse_mode: str,
    checkpoint: Path,
) -> dict[str, Any]:
    values = {name: float(raw_prediction[index]) for index, name in enumerate(label_names)}
    chart_combo = int(getattr(parsed, "playable_note_count", 0) or 0)
    row_combo = row.get("ese", {}).get("note_count") or row.get("combo")
    combo = chart_combo or round_or_int(row_combo) or round_or_int(values.get("combo"))
    const = round(clamp(numeric(values.get("const")), 1.0, 11.8), 1)
    features = {
        key: round(clamp(numeric(values.get(key)), 0.0, 100.0), 2)
        for key in FEATURE_KEYS
    }
    roll_seconds = clamp(numeric(values.get("roll_time")), 0.0, 60.0)
    balloon_num = min(round_or_int(values.get("balloon_num")), 400)
    predicted_combo = round_or_int(values.get("combo"))
    try:
        checkpoint_ref = str(checkpoint.relative_to(WORKSPACE_ROOT))
    except ValueError:
        checkpoint_ref = str(checkpoint)

    return {
        "const": const,
        "combo": combo,
        "features": features,
        "roll_time": format_roll_time(roll_seconds),
        "roll_time_seconds": round(roll_seconds, 3),
        "balloon_num": balloon_num,
        "encoder": {
            "status": "ok",
            "model": "encoder_v1",
            "checkpoint": checkpoint_ref,
            "parse_mode": parse_mode,
            "duration_frames": int(grid.duration_frames),
            "clipped": bool(grid.clipped),
            "combo_predicted": predicted_combo,
        },
    }


def load_model(checkpoint_path: Path, device: torch.device) -> torch.nn.Module:
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=False)
    model = build_model(checkpoint)
    model.to(device)
    model.eval()
    return model


def predict_batch(
    batch: list[dict[str, Any]],
    model: torch.nn.Module,
    device: torch.device,
    stats: dict[str, Any],
) -> np.ndarray:
    x = np.stack([item["grid"].x.transpose(1, 0) for item in batch]).astype(np.float32)
    tensor = torch.from_numpy(x).to(device)
    with torch.no_grad():
        output = model(tensor)
        if isinstance(output, dict):
            output = output["regression"]
        pred_norm = output.detach().cpu().numpy()
    return inverse_targets(pred_norm, stats)


def process_batch(
    batch: list[dict[str, Any]],
    model: torch.nn.Module,
    device: torch.device,
    stats: dict[str, Any],
    output: dict[str, Any],
    errors: list[dict[str, str]],
    checkpoint: Path,
) -> int:
    if not batch:
        return 0
    try:
        pred_raw = predict_batch(batch, model, device, stats)
    except Exception as exc:  # noqa: BLE001 - record individual fallback errors.
        for item in batch:
            errors.append(
                {
                    "id": str(item["row"].get("id")),
                    "title": str(item["row"].get("title")),
                    "course": str(item["row"].get("course")),
                    "error": f"prediction failed: {type(exc).__name__}: {exc}",
                }
            )
        return 0

    label_names = [str(name) for name in stats["label_names"]]
    generated = 0
    for item, raw_prediction in zip(batch, pred_raw):
        row = item["row"]
        chart_id = str(row.get("id"))
        output[chart_id] = prediction_to_stats(
            row,
            raw_prediction,
            label_names,
            item["parsed"],
            item["grid"],
            item["parse_mode"],
            checkpoint,
        )
        generated += 1
    return generated


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Generate missing Taiko chart stats with encoder_v1.")
    parser.add_argument("--chart-data", type=Path, default=PROJECT_ROOT / "data" / "chart_data.json")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "data" / "encoder_chart_stats.json")
    parser.add_argument("--summary", type=Path, default=PROJECT_ROOT / "data" / "encoder_chart_stats_summary.json")
    parser.add_argument("--config", type=Path, default=DIFFUSION_ROOT / "configs" / "encoder_v1.yaml")
    parser.add_argument("--checkpoint", type=Path, default=DIFFUSION_ROOT / "checkpoints" / "encoder_v1" / "best.pt")
    parser.add_argument("--stats", type=Path, default=DIFFUSION_ROOT / "data" / "splits" / "encoder_v1" / "label_stats.json")
    parser.add_argument("--batch-size", type=int, default=24)
    parser.add_argument("--device", default="auto", choices=["auto", "cpu", "cuda"])
    parser.add_argument("--include-excel", action="store_true", help="Also generate encoder stats for xlsx-backed rows.")
    parser.add_argument("--overwrite", action="store_true", help="Ignore existing generated stats.")
    parser.add_argument("--limit", type=int, default=0, help="Optional small run for validation.")
    args = parser.parse_args()

    config = load_config(args.config)
    data_config = config.get("data", {})
    grid_config = config.get("chart_grid", {})
    frame_ms = float(data_config.get("frame_ms", 46.4399))
    max_frames = int(data_config.get("max_frames", 8192))
    discard_branch = bool(data_config.get("discard_branch", True))
    channels = [str(channel) for channel in grid_config["channels"]]
    stats = read_json(args.stats, {})
    if not stats:
        raise FileNotFoundError(args.stats)

    requested = args.device
    device_name = "cuda" if requested == "auto" and torch.cuda.is_available() else requested
    if device_name == "auto":
        device_name = "cpu"
    device = torch.device(device_name)
    model = load_model(args.checkpoint, device)

    charts = read_json(args.chart_data, [])
    existing = {} if args.overwrite else read_json(args.output, {})
    output: dict[str, Any] = dict(existing)
    selected = [row for row in charts if args.include_excel or is_missing_chart(row)]
    if args.limit > 0:
        selected = selected[: args.limit]

    start = time.time()
    generated = 0
    skipped_existing = 0
    errors: list[dict[str, str]] = []
    by_course = Counter()
    batch: list[dict[str, Any]] = []

    for row in selected:
        chart_id = str(row.get("id"))
        if chart_id in output and not args.overwrite:
            skipped_existing += 1
            continue
        try:
            parsed, _path, parse_mode = parse_chart(row, discard_branch)
            grid = chart_to_grid(parsed, frame_ms=frame_ms, max_frames=max_frames, channels=channels)
            batch.append({"row": row, "parsed": parsed, "grid": grid, "parse_mode": parse_mode})
            by_course[str(row.get("course"))] += 1
        except Exception as exc:  # noqa: BLE001 - keep generating the rest.
            errors.append(
                {
                    "id": chart_id,
                    "title": str(row.get("title")),
                    "course": str(row.get("course")),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

        if len(batch) >= max(1, args.batch_size):
            generated += process_batch(batch, model, device, stats, output, errors, args.checkpoint)
            batch.clear()
            if generated and generated % 240 == 0:
                print(f"generated {generated}/{len(selected)}")

    generated += process_batch(batch, model, device, stats, output, errors, args.checkpoint)

    elapsed = time.time() - start
    summary = {
        "input_charts": len(charts),
        "selected": len(selected),
        "generated": generated,
        "skipped_existing": skipped_existing,
        "errors": len(errors),
        "by_course": dict(sorted(by_course.items())),
        "device": str(device),
        "model": "encoder_v1",
        "checkpoint": str(args.checkpoint),
        "elapsed_seconds": round(elapsed, 2),
        "error_samples": errors[:50],
    }

    write_json(args.output, output)
    write_json(args.summary, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
