#!/usr/bin/env python3
"""Privacy-safe, append-only MAS performance telemetry.

Each telemetry JSONL file is deliberately one anonymised run sample.  It may
contain several phase/task events, but never stores a run, task, meeting, or
source identifier.  The aggregate command therefore receives a list of sample
files rather than trying to reconstruct runs from identifiers.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
from pathlib import Path
from typing import Any, Iterator


SCHEMA_VERSION = "mas_performance_telemetry.v1"
EVENT_TYPES = {
    "phase_queue",
    "phase_start",
    "phase_end",
    "task_queue",
    "task_start",
    "task_end",
    "ingest",
    "assembly",
}
SOURCE_MODES = {"document_only", "audio_only", "audio_plus_document"}
MEETING_TYPES = {"expert_call", "listed_company", "group_review", "other"}
SIZE_PROFILES = {"small", "medium", "large"}
RISK_PROFILES = {"low", "medium", "high"}
EDITING_MODES = {"direct", "full", "single", "parallel", "not_applicable"}
PHASES = {"pre_draft", "editing", "draft_review", "final_verification", "complete", "not_applicable"}
TASK_KINDS = {
    "operator",
    "specialist_return",
    "speaker_editing",
    "entity_verification",
    "fidelity_review",
    "deterministic_validation",
    "not_applicable",
}
SAMPLE_KINDS = {"production", "synthetic", "non_production"}
COUNT_FIELDS = ("candidate_count", "group_count", "shard_count", "retry_count")
MEASURE_FIELDS = ("duration_ms", "queue_ms")
REQUIRED_FIELDS = {
    "schema_version",
    "event_type",
    "source_mode",
    "meeting_type",
    "size_profile",
    "risk_profile",
    "editing_mode",
    "sample_kind",
    "phase",
    "task_kind",
    *COUNT_FIELDS,
    *MEASURE_FIELDS,
}
PROFILE_FIELDS = (
    "source_mode",
    "meeting_type",
    "size_profile",
    "risk_profile",
    "editing_mode",
    "sample_kind",
)
CALIBRATION_MINIMUM_SAMPLES = 3


@contextmanager
def telemetry_lock(log_path: Path) -> Iterator[None]:
    """Lock only the public telemetry log next to the append target."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = log_path.with_name(log_path.name + ".lock")
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _is_nonnegative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def validate_event(event: Any) -> dict[str, Any]:
    """Validate a telemetry event using an exact schema (unknown fields fail closed)."""
    if not isinstance(event, dict):
        raise ValueError("telemetry event must be a JSON object")
    keys = set(event)
    missing = sorted(REQUIRED_FIELDS - keys)
    unknown = sorted(keys - REQUIRED_FIELDS)
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append("missing fields: " + ", ".join(missing))
        if unknown:
            parts.append("unknown fields: " + ", ".join(unknown))
        raise ValueError("invalid telemetry event schema; " + "; ".join(parts))
    if event["schema_version"] != SCHEMA_VERSION:
        raise ValueError("telemetry schema_version is unsupported")
    enum_checks = {
        "event_type": EVENT_TYPES,
        "source_mode": SOURCE_MODES,
        "meeting_type": MEETING_TYPES,
        "size_profile": SIZE_PROFILES,
        "risk_profile": RISK_PROFILES,
        "editing_mode": EDITING_MODES,
        "sample_kind": SAMPLE_KINDS,
        "phase": PHASES,
        "task_kind": TASK_KINDS,
    }
    for field, allowed in enum_checks.items():
        if event[field] not in allowed:
            raise ValueError(f"telemetry {field} is not an allowed enum value")
    for field in COUNT_FIELDS:
        if not _is_nonnegative_int(event[field]):
            raise ValueError(f"telemetry {field} must be a non-negative integer")
    for field in MEASURE_FIELDS:
        if not _is_nonnegative_number(event[field]):
            raise ValueError(f"telemetry {field} must be a finite non-negative number")
    return {key: event[key] for key in sorted(REQUIRED_FIELDS)}


def append_event(log_path: Path, event: dict[str, Any]) -> None:
    """Append one validated JSONL record while holding an adjacent lock."""
    normalized = validate_event(event)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    with telemetry_lock(log_path):
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())


def read_sample_events(path: Path) -> list[dict[str, Any]]:
    """Read one independent JSON or JSONL sample without retaining its path in output."""
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise ValueError("telemetry sample is empty")
    if path.suffix.lower() == ".json":
        payload = json.loads(raw)
        raw_events = payload if isinstance(payload, list) else [payload]
    else:
        raw_events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not raw_events:
        raise ValueError("telemetry sample has no events")
    return [validate_event(item) for item in raw_events]


def percentile(values: list[float], point: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * point
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def metric_summary(values: list[float]) -> dict[str, float | int] | None:
    if not values:
        return None
    return {
        "count": len(values),
        "mean": round(sum(values) / len(values), 3),
        "p50": round(percentile(values, 0.50) or 0.0, 3),
        "p95": round(percentile(values, 0.95) or 0.0, 3),
    }


def sample_profile(events: list[dict[str, Any]]) -> tuple[str, ...]:
    profiles = {tuple(str(event[field]) for field in PROFILE_FIELDS) for event in events}
    if len(profiles) != 1:
        raise ValueError("telemetry sample contains inconsistent privacy-safe profiles")
    return next(iter(profiles))


def is_complete_run(events: list[dict[str, Any]]) -> bool:
    starts = {str(event["phase"]) for event in events if event["event_type"] == "phase_start"}
    ends = {str(event["phase"]) for event in events if event["event_type"] == "phase_end"}
    return "pre_draft" in starts and "complete" in ends


def aggregate_samples(sample_paths: list[Path]) -> dict[str, Any]:
    if not sample_paths:
        raise ValueError("at least one independent telemetry sample is required")
    per_mode: dict[str, dict[str, Any]] = {
        mode: {
            "observed_sample_files": 0,
            "valid_complete_runs": 0,
            "excluded_non_production_samples": 0,
            "invalid_sample_files": 0,
            "event_metrics": {event_type: {"duration_ms": [], "queue_ms": []} for event_type in sorted(EVENT_TYPES)},
        }
        for mode in sorted(SOURCE_MODES)
    }
    invalid_without_mode = 0
    for path in sample_paths:
        try:
            events = read_sample_events(path)
            profile = sample_profile(events)
            source_mode, _, _, _, _, sample_kind = profile
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            invalid_without_mode += 1
            continue
        state = per_mode[source_mode]
        state["observed_sample_files"] += 1
        if sample_kind != "production":
            state["excluded_non_production_samples"] += 1
            continue
        if not is_complete_run(events):
            state["invalid_sample_files"] += 1
            continue
        state["valid_complete_runs"] += 1
        for event in events:
            metrics = state["event_metrics"][event["event_type"]]
            metrics["duration_ms"].append(float(event["duration_ms"]))
            metrics["queue_ms"].append(float(event["queue_ms"]))

    mode_reports: list[dict[str, Any]] = []
    for source_mode in sorted(SOURCE_MODES):
        state = per_mode[source_mode]
        valid_runs = int(state["valid_complete_runs"])
        ready = valid_runs >= CALIBRATION_MINIMUM_SAMPLES
        event_metrics: dict[str, Any] = {}
        for event_type in sorted(EVENT_TYPES):
            raw_metrics = state["event_metrics"][event_type]
            summary = {
                "duration_ms": metric_summary(raw_metrics["duration_ms"]),
                "queue_ms": metric_summary(raw_metrics["queue_ms"]),
            }
            if any(value is not None for value in summary.values()):
                event_metrics[event_type] = summary
        mode_reports.append(
            {
                "source_mode": source_mode,
                "observed_sample_files": state["observed_sample_files"],
                "valid_complete_runs": valid_runs,
                "excluded_non_production_samples": state["excluded_non_production_samples"],
                "invalid_sample_files": state["invalid_sample_files"],
                "calibration_minimum_samples": CALIBRATION_MINIMUM_SAMPLES,
                "calibration_status": "ready" if ready else "insufficient_data",
                "review_required": True,
                "threshold_change_applied": False,
                "event_metrics": event_metrics,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "aggregate_scope": "independent_anonymized_sample_files",
        "sample_files_received": len(sample_paths),
        "invalid_sample_files_without_usable_mode": invalid_without_mode,
        "threshold_change_applied": False,
        "source_mode_reports": mode_reports,
    }


def build_event_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "event_type": args.event_type,
        "source_mode": args.source_mode,
        "meeting_type": args.meeting_type,
        "size_profile": args.size_profile,
        "risk_profile": args.risk_profile,
        "editing_mode": args.editing_mode,
        "sample_kind": args.sample_kind,
        "phase": args.phase,
        "task_kind": args.task_kind,
        "candidate_count": args.candidate_count,
        "group_count": args.group_count,
        "shard_count": args.shard_count,
        "retry_count": args.retry_count,
        "duration_ms": args.duration_ms,
        "queue_ms": args.queue_ms,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record or aggregate privacy-safe MAS performance telemetry")
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record", help="append one anonymous telemetry event")
    record.add_argument("--telemetry-jsonl", required=True, type=Path)
    record.add_argument("--event-type", required=True, choices=sorted(EVENT_TYPES))
    record.add_argument("--source-mode", required=True, choices=sorted(SOURCE_MODES))
    record.add_argument("--meeting-type", required=True, choices=sorted(MEETING_TYPES))
    record.add_argument("--size-profile", required=True, choices=sorted(SIZE_PROFILES))
    record.add_argument("--risk-profile", required=True, choices=sorted(RISK_PROFILES))
    record.add_argument("--editing-mode", required=True, choices=sorted(EDITING_MODES))
    record.add_argument("--sample-kind", default="production", choices=sorted(SAMPLE_KINDS))
    record.add_argument("--phase", required=True, choices=sorted(PHASES))
    record.add_argument("--task-kind", required=True, choices=sorted(TASK_KINDS))
    for field in COUNT_FIELDS:
        record.add_argument("--" + field.replace("_", "-"), required=True, type=int)
    for field in MEASURE_FIELDS:
        record.add_argument("--" + field.replace("_", "-"), required=True, type=float)
    record.add_argument("--json", action="store_true")
    aggregate = subparsers.add_parser("aggregate", help="aggregate independent telemetry sample files")
    aggregate.add_argument("--sample", required=True, action="append", type=Path)
    aggregate.add_argument("--json", action="store_true")
    return parser.parse_args()


def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return
    if "source_mode_reports" in payload:
        print("MAS performance telemetry aggregate")
        for item in payload["source_mode_reports"]:
            print(
                f"{item['source_mode']}: {item['calibration_status']} "
                f"({item['valid_complete_runs']} valid complete production samples)"
            )
        return
    print("telemetry event recorded")


def main() -> int:
    args = parse_args()
    try:
        if args.command == "record":
            append_event(args.telemetry_jsonl, build_event_from_args(args))
            emit({"ok": True, "schema_version": SCHEMA_VERSION, "recorded": 1}, as_json=args.json)
        else:
            report = aggregate_samples(args.sample)
            emit(report, as_json=args.json)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        error = {"ok": False, "schema_version": SCHEMA_VERSION, "error": str(exc)}
        emit(error, as_json=getattr(args, "json", False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
