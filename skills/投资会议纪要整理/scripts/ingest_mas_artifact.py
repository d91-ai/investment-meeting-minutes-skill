#!/usr/bin/env python3
"""Ingest one MAS subagent artifact return into a dispatch directory."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from collect_mas_artifacts import PHASE_ORDER
from validate_mas_artifacts import (
    MAIN_OWNED_ARTIFACTS,
    artifact_mapping,
    read_json,
    validate_dispatch_identity,
    validate_payload,
)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return cleaned.strip("-") or "artifact"


def load_json_input(input_path: Path | None) -> tuple[Any | None, str, str, list[str]]:
    if input_path is None:
        source = "stdin"
        try:
            raw_text = sys.stdin.read()
        except OSError as exc:
            return None, source, "", [f"无法读取 subagent artifact JSON: {exc}"]
    else:
        source = str(input_path)
        try:
            raw_text = input_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return None, source, "", [f"无法读取 subagent artifact JSON: {exc}"]
    try:
        return json.loads(raw_text), source, raw_text, []
    except json.JSONDecodeError as exc:
        return None, source, raw_text, [f"无法解析 subagent artifact JSON: {exc}"]


def collector_command(task_dir: Path, through_phase: str | None = None) -> str:
    script_path = Path(__file__).with_name("collect_mas_artifacts.py")
    command = f"python3 {shlex.quote(str(script_path))} {shlex.quote(str(task_dir))} --json"
    if through_phase:
        command = (
            f"python3 {shlex.quote(str(script_path))} {shlex.quote(str(task_dir))} "
            f"--through-phase {shlex.quote(through_phase)} --json"
        )
    return command


def repair_file_path(repair_dir: Path, reason: str, artifact_types: list[str], input_source: str) -> Path:
    source_name = "stdin" if input_source == "stdin" else Path(input_source).stem
    type_part = "-".join(safe_name(artifact_type) for artifact_type in artifact_types) if artifact_types else "unknown"
    import uuid

    return repair_dir / f"{utc_stamp()}-{uuid.uuid4().hex[:12]}-{type_part}-{safe_name(reason)}-{safe_name(source_name)}.json"


def write_repair_record(
    repair_dir: Path,
    reason: str,
    input_source: str,
    payload: Any,
    errors: list[str],
    warnings: list[str],
    artifact_types: list[str],
) -> Path:
    path = repair_file_path(repair_dir, reason, artifact_types, input_source)
    write_json(
        path,
        {
            "schema_version": "1.0",
            "ingest_status": reason,
            "input_source": input_source,
            "artifact_types": artifact_types,
            "errors": errors,
            "warnings": warnings,
            "payload": payload,
        },
    )
    return path


def ingest_mas_artifact(
    payload: Any,
    task_dir: Path,
    input_source: str = "stdin",
    through_phase: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    task_dir = task_dir.expanduser()
    artifact_dir = task_dir / "artifacts"
    repair_dir = task_dir / "repair_history"
    artifacts, mapping_errors = artifact_mapping(payload)
    validation = validate_payload(payload)
    errors = [str(error) for error in mapping_errors]
    for error in validation.get("errors", []):
        if str(error) not in errors:
            errors.append(str(error))
    warnings = [str(warning) for warning in validation.get("warnings", [])]
    artifact_types = sorted(str(artifact_type) for artifact_type in artifacts)
    main_owned_types = sorted(set(artifact_types) & MAIN_OWNED_ARTIFACTS)
    if main_owned_types:
        errors.append(
            "subagent ingest 不接受 Main Orchestrator 自有 artifact: " + ", ".join(main_owned_types)
        )
    written_artifacts: list[dict[str, str]] = []
    repair_history_file = ""
    ingest_status = "written"

    try:
        bundle = read_json(task_dir / "mas_task_bundle.json")
        manifest = read_json(task_dir / "dispatch_manifest.json")
        if not isinstance(bundle, dict) or not isinstance(manifest, dict):
            raise ValueError("MAS bundle and dispatch manifest must be JSON objects")
        errors.extend(
            validate_dispatch_identity(
                payload,
                bundle,
                manifest,
                through_phase=through_phase,
                phase_order=PHASE_ORDER,
            )
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        errors.append(f"无法校验 MAS artifact dispatch identity: {exc}")

    if errors:
        ingest_status = "invalid_artifact_not_written"
        repair_history_file = str(
            write_repair_record(
                repair_dir,
                "invalid",
                input_source,
                payload,
                errors,
                warnings,
                artifact_types,
            )
        )
    else:
        duplicate_paths = [
            artifact_dir / f"{artifact_type}.json"
            for artifact_type in artifact_types
            if (artifact_dir / f"{artifact_type}.json").exists()
        ]
        if duplicate_paths and not replace_existing:
            ingest_status = "duplicate_artifact_not_written"
            errors.extend(f"artifact already exists: {path}" for path in duplicate_paths)
            repair_history_file = str(
                write_repair_record(
                    repair_dir,
                    "duplicate",
                    input_source,
                    payload,
                    errors,
                    warnings,
                    artifact_types,
                )
            )
        else:
            if duplicate_paths:
                for path in duplicate_paths:
                    try:
                        previous_payload = read_json(path)
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                        errors.append(f"无法归档待替换 MAS artifact: {path}: {exc}")
                        continue
                    repair_history_file = str(
                        write_repair_record(
                            repair_dir,
                            "superseded",
                            str(path),
                            previous_payload,
                            [],
                            ["explicit replacement requested by Main Orchestrator"],
                            artifact_types,
                        )
                    )
                if errors:
                    ingest_status = "replacement_archive_failed_not_written"
                    return {
                        "schema_version": "1.0",
                        "ok": False,
                        "ingest_status": ingest_status,
                        "input_source": input_source,
                        "task_dir": str(task_dir),
                        "artifact_dir": str(artifact_dir),
                        "repair_history_dir": str(repair_dir),
                        "artifact_types": artifact_types,
                        "written_artifacts": [],
                        "repair_history_file": repair_history_file,
                        "validation": validation,
                        "next_collector_command": collector_command(task_dir, through_phase=through_phase),
                        "errors": errors,
                        "warnings": warnings,
                    }
                ingest_status = "replaced"
            for artifact_type in artifact_types:
                path = artifact_dir / f"{artifact_type}.json"
                write_json(
                    path,
                    {
                        "run_id": payload.get("run_id"),
                        "task_id": payload.get("task_id"),
                        "dispatch_phase": payload.get("dispatch_phase"),
                        "artifact_owner": payload.get("artifact_owner"),
                        "artifact_type": artifact_type,
                        "artifact": artifacts[artifact_type],
                    },
                )
                written_artifacts.append({"artifact_type": artifact_type, "path": str(path)})

    ok = ingest_status in {"written", "replaced"}
    return {
        "schema_version": "1.0",
        "ok": ok,
        "ingest_status": ingest_status,
        "input_source": input_source,
        "task_dir": str(task_dir),
        "artifact_dir": str(artifact_dir),
        "repair_history_dir": str(repair_dir),
        "artifact_types": artifact_types,
        "written_artifacts": written_artifacts,
        "repair_history_file": repair_history_file,
        "validation": validation,
        "next_collector_command": collector_command(task_dir, through_phase=through_phase),
        "errors": errors,
        "warnings": warnings,
    }


def ingest_mas_artifact_file(
    input_path: Path | None,
    task_dir: Path,
    through_phase: str | None = None,
    replace_existing: bool = False,
) -> dict[str, Any]:
    payload, input_source, raw_text, parse_errors = load_json_input(input_path)
    if parse_errors:
        task_dir = task_dir.expanduser()
        repair_dir = task_dir / "repair_history"
        repair_path = write_repair_record(
            repair_dir,
            "parse_error",
            input_source,
            {"raw_text": raw_text},
            parse_errors,
            [],
            [],
        )
        return {
            "schema_version": "1.0",
            "ok": False,
            "ingest_status": "parse_error_not_written",
            "input_source": input_source,
            "task_dir": str(task_dir),
            "artifact_dir": str(task_dir / "artifacts"),
            "repair_history_dir": str(repair_dir),
            "artifact_types": [],
            "written_artifacts": [],
            "repair_history_file": str(repair_path),
            "validation": {"ok": False, "errors": parse_errors, "warnings": []},
            "next_collector_command": collector_command(task_dir, through_phase=through_phase),
            "errors": parse_errors,
            "warnings": [],
        }
    return ingest_mas_artifact(
        payload,
        task_dir,
        input_source=input_source,
        through_phase=through_phase,
        replace_existing=replace_existing,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="接收并校验一个 MAS subagent artifact JSON 返回")
    parser.add_argument("artifact_file", nargs="?", help="subagent 返回的 JSON 文件；省略时从 stdin 读取")
    parser.add_argument("--task-dir", required=True, help="包含 MAS dispatch files 的任务目录")
    parser.add_argument("--through-phase", choices=sorted(PHASE_ORDER), help="建议下一次 collector 校验到的 phase")
    parser.add_argument("--replace-existing", action="store_true", help="显式替换同 run/task 的已有 artifact，并先归档旧值")
    parser.add_argument("--json", action="store_true", help="输出 JSON；默认也是 JSON")
    args = parser.parse_args()

    input_path = Path(args.artifact_file) if args.artifact_file else None
    result = ingest_mas_artifact_file(
        input_path,
        Path(args.task_dir),
        through_phase=args.through_phase,
        replace_existing=bool(args.replace_existing),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
