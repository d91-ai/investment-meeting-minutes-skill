#!/usr/bin/env python3
"""Create a deterministic staged MAS dry run from synthetic artifacts."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from build_mas_task_bundle import build_bundle_from_request, validate_bundle, write_dispatch_files
from assemble_speaker_turn_edits import assemble_speaker_turn_edits
from assemble_entity_verification_shards import assemble_entity_verification_shards
from collect_mas_artifacts import collect_mas_run, merge_artifact_files, required_artifacts_for_phase
from create_mas_source_manifest import create_source_manifest
from record_mas_main_actions import record_main_actions
from build_deterministic_export_manifest import build_deterministic_export_manifest
from validate_mas_artifacts import file_sha256

PHASES = ("pre_draft", "editing", "draft_review", "final_verification")
DRY_RUN_MARKER = ".mas-dry-run-marker"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def synthetic_final_markdown(artifacts: dict[str, Any]) -> str:
    doubtful_items = artifacts.get("doubtful_items")
    first = (
        doubtful_items[0]
        if isinstance(doubtful_items, list) and doubtful_items and isinstance(doubtful_items[0], dict)
        else None
    )
    lines = [
        "# 投资会议纪要｜合成 MAS dry-run",
        "",
        "**会议日期**：2026-07-11",
        "**整理时间**：2026-07-11",
        "**会议标题**：合成 MAS dry-run 会议",
        "**会议类型**：多人复盘会",
        "**会议系列**：合成回归",
        "",
        "---",
        "",
        "## 一、发言整理",
        "",
        "### 发言人1",
        "",
        "#### 【合成回归】",
        "",
    ]
    if first is None:
        lines.append("我按当前会话原文保留这段合成回归内容。")
    else:
        raw = str(first.get("原始表述") or "合成存疑词").replace("|", "\\|")
        current = str(first.get("当前判断") or "待人工确认").replace("|", "\\|")
        candidate = str(first.get("候选项") or "").replace("|", "\\|")
        lines.extend(
            [
                f"我在当前会话中提到 **{raw}**，需要保留原始存疑。",
                "",
                "## 二、存疑与待确认",
                "",
                "| 原始表述 | 当前判断 | 候选项 | 人工确认 |",
                "| --- | --- | --- | --- |",
                f"| {raw} | {current} | {candidate} | |",
            ]
        )
    return "\n".join(lines) + "\n"


def synthetic_verification_payload(artifacts: dict[str, Any]) -> dict[str, Any]:
    doubtful_items = artifacts.get("doubtful_items")
    records = [
        copy.deepcopy(item)
        for item in doubtful_items
        if isinstance(doubtful_items, list)
        and isinstance(item, dict)
        and item.get("是否需要 sidecar") is True
    ] if isinstance(doubtful_items, list) else []
    return {"records": records}


def can_overwrite_task_dir(task_dir: Path) -> bool:
    resolved = task_dir.expanduser().resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root not in resolved.parents and resolved != temp_root:
        return False
    if not resolved.name.startswith("mas-"):
        return False
    return (
        (resolved / DRY_RUN_MARKER).exists()
        or (resolved / "mas_task_bundle.json").exists()
        or (resolved / "dispatch_manifest.json").exists()
    )


def load_fixture_artifacts(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("artifacts"), dict):
        raise ValueError(f"MAS dry-run fixture must contain an artifacts object: {path}")
    return dict(payload["artifacts"])


def artifact_identity(manifest: dict[str, Any], artifact_type: str) -> dict[str, Any]:
    run_id = str(manifest.get("run_id") or "")
    if artifact_type in {"source_manifest", "export_manifest"}:
        return {
            "run_id": run_id,
            "task_id": f"{run_id}:main:{artifact_type}",
            "dispatch_phase": "pre_draft" if artifact_type == "source_manifest" else "final_verification",
            "artifact_owner": "Main Orchestrator",
        }
    if artifact_type == "editing_assembly_receipt":
        return {
            "run_id": run_id,
            "task_id": f"{run_id}:main:editing_assembly_receipt",
            "dispatch_phase": "editing",
            "artifact_owner": "Main Orchestrator",
        }
    for task in manifest.get("task_files", []):
        if not isinstance(task, dict):
            continue
        produced = {str(task.get("artifact_type") or "")}
        produced.update(str(item) for item in task.get("secondary_artifacts", []))
        if artifact_type in produced:
            return {
                "run_id": run_id,
                "task_id": str(task.get("task_id") or ""),
                "dispatch_phase": str(task.get("dispatch_phase") or ""),
                "artifact_owner": str(task.get("artifact_owner") or task.get("role") or ""),
            }
    raise ValueError(f"MAS dry-run cannot resolve task identity for artifact: {artifact_type}")


def write_artifact(
    artifact_dir: Path,
    manifest: dict[str, Any],
    artifact_type: str,
    artifact: Any,
) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{artifact_type}.json"
    write_json(
        path,
        {
            **artifact_identity(manifest, artifact_type),
            "artifact_type": artifact_type,
            "artifact": artifact,
        },
    )
    return path


def task_files_by_phase(dispatch_manifest: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {phase: [] for phase in PHASES}
    for item in dispatch_manifest.get("task_files", []):
        if not isinstance(item, dict):
            continue
        phase = str(item.get("dispatch_phase") or "")
        if phase not in grouped:
            continue
        grouped[phase].append(
            {
                "role": str(item.get("role") or ""),
                "artifact_type": str(item.get("artifact_type") or ""),
                "path": str(item.get("path") or ""),
            }
        )
    return grouped


def run_mas_dry_run(request_path: Path, artifact_fixture_path: Path, task_dir: Path) -> dict[str, Any]:
    request = read_json(request_path)
    if not isinstance(request, dict):
        raise ValueError(f"MAS dry-run request must be a JSON object: {request_path}")
    fixture_artifacts = copy.deepcopy(load_fixture_artifacts(artifact_fixture_path))
    bundle = build_bundle_from_request(request)
    errors = validate_bundle(bundle)

    dispatch_result = write_dispatch_files(bundle, task_dir)
    manifest_path = Path(dispatch_result["manifest_file"])
    manifest = read_json(manifest_path)
    bound_bundle = read_json(Path(dispatch_result["bundle_file"]))
    if not isinstance(bound_bundle, dict) or not isinstance(manifest, dict):
        raise ValueError("MAS dry-run dispatch bundle and manifest must be JSON objects")
    bundle = bound_bundle
    artifact_dir = task_dir / "artifacts"
    synthetic_markdown = task_dir / "synthetic-final.md"
    synthetic_markdown.write_text(synthetic_final_markdown(fixture_artifacts), encoding="utf-8")
    write_json(
        task_dir / "synthetic.verification.json",
        synthetic_verification_payload(fixture_artifacts),
    )
    emitted_artifacts: set[str] = set()
    artifact_files: list[dict[str, str]] = []
    phase_results: list[dict[str, Any]] = []
    grouped_task_files = task_files_by_phase(manifest if isinstance(manifest, dict) else {})
    stop_reason = "completed"

    for phase_index, phase in enumerate(PHASES):
        required_now = required_artifacts_for_phase(bundle, phase)
        entity_parallel = (
            isinstance(bundle.get("entity_verification"), dict)
            and bundle["entity_verification"].get("effective_mode") == "parallel"
        )
        emitted_this_phase: list[str] = []
        for artifact_type in required_now:
            if artifact_type in emitted_artifacts:
                continue
            if artifact_type == "editing_assembly_receipt":
                continue
            if entity_parallel and artifact_type in {
                "entity_verification_report",
                "doubtful_items",
                "entity_verification_assembly_receipt",
            }:
                continue
            if artifact_type == "source_manifest":
                artifact, source_warnings = create_source_manifest(bundle)
                errors.extend(str(item) for item in source_warnings if str(item).startswith("ERROR:"))
            elif artifact_type == "export_manifest":
                validator_paths: list[Path] = []
                for validator_name in ("validate_utf8_text.py", "validate_meeting_minutes_contract.py"):
                    evidence_path = task_dir / f"synthetic.{validator_name}.json"
                    write_json(evidence_path, {"name": validator_name, "ok": True})
                    validator_paths.append(evidence_path)
                regression_path = task_dir / "synthetic.run_meeting_minutes_regression.py.json"
                write_json(
                    regression_path,
                    {"name": "run_meeting_minutes_regression.py", "case_count": 1, "ok": True},
                )
                export_result = build_deterministic_export_manifest(
                    task_dir,
                    synthetic_markdown,
                    verification_sidecar_path=(
                        task_dir / "synthetic.verification.json"
                        if synthetic_verification_payload(fixture_artifacts).get("records")
                        else None
                    ),
                    validator_evidence_paths=validator_paths,
                    regression_evidence_path=regression_path,
                )
                artifact_path = Path(str(export_result["artifact_file"]))
                emitted_artifacts.add(artifact_type)
                emitted_this_phase.append(artifact_type)
                artifact_files.append({"artifact_type": artifact_type, "path": str(artifact_path)})
                continue
            elif artifact_type in fixture_artifacts:
                artifact = copy.deepcopy(fixture_artifacts[artifact_type])
            elif artifact_type.startswith(("speaker_turn_edit__", "entity_verification_shard__")):
                task = next(
                    (
                        item
                        for item in bundle.get("tasks", [])
                        if isinstance(item, dict) and item.get("artifact_type") == artifact_type
                    ),
                    None,
                )
                shape = task.get("expected_output_shape") if isinstance(task, dict) else None
                if artifact_type.startswith("entity_verification_shard__") and isinstance(shape, dict):
                    from ingest_mas_artifact import expand_entity_verification_response

                    expanded = expand_entity_verification_response(shape, task_dir)
                    artifact = expanded.get("artifact")
                else:
                    artifact = shape.get("artifact") if isinstance(shape, dict) else None
                if not isinstance(artifact, dict):
                    errors.append(f"MAS dry-run cannot synthesize shard artifact: {artifact_type}")
                    continue
                artifact = copy.deepcopy(artifact)
                if artifact_type.startswith("speaker_turn_edit__"):
                    context_turns = {
                        str(turn.get("turn_id") or ""): turn
                        for turn in task.get("task_context", {}).get("turns", [])
                        if isinstance(turn, dict)
                    }
                    for returned_turn in artifact.get("edited_turns", []):
                        if not isinstance(returned_turn, dict):
                            continue
                        source_turn = context_turns.get(str(returned_turn.get("turn_id") or ""))
                        if isinstance(source_turn, dict):
                            returned_turn["edited_text"] = str(source_turn.get("text") or "")
            else:
                errors.append(f"MAS dry-run fixture missing artifact: {artifact_type}")
                continue
            artifact_path = write_artifact(artifact_dir, manifest, artifact_type, artifact)
            emitted_artifacts.add(artifact_type)
            emitted_this_phase.append(artifact_type)
            artifact_files.append({"artifact_type": artifact_type, "path": str(artifact_path)})
        if phase == "pre_draft" and entity_parallel:
            try:
                entity_result = assemble_entity_verification_shards(task_dir)
                for artifact_type, path in entity_result.get("artifacts", {}).items():
                    emitted_artifacts.add(str(artifact_type))
                    emitted_this_phase.append(str(artifact_type))
                    artifact_files.append({"artifact_type": str(artifact_type), "path": str(path)})
                doubtful_path = Path(str(entity_result.get("artifacts", {}).get("doubtful_items") or ""))
                doubtful_envelope = read_json(doubtful_path)
                if isinstance(doubtful_envelope, dict) and isinstance(doubtful_envelope.get("artifact"), list):
                    fixture_artifacts["doubtful_items"] = copy.deepcopy(doubtful_envelope["artifact"])
                    synthetic_markdown.write_text(synthetic_final_markdown(fixture_artifacts), encoding="utf-8")
                    write_json(
                        task_dir / "synthetic.verification.json",
                        synthetic_verification_payload(fixture_artifacts),
                    )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"MAS dry-run entity verification assembly failed: {exc}")
        if phase == "editing" and "editing_assembly_receipt" in required_now:
            try:
                assembly_result = assemble_speaker_turn_edits(task_dir)
                emitted_artifacts.add("editing_assembly_receipt")
                emitted_this_phase.append("editing_assembly_receipt")
                artifact_files.append(
                    {
                        "artifact_type": "editing_assembly_receipt",
                        "path": str(assembly_result["receipt"]),
                    }
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                errors.append(f"MAS dry-run speaker edit assembly failed: {exc}")

        summary = collect_mas_run(task_dir, through_phase=phase)
        summary_path = task_dir / f"mas_run_summary.{phase}.json"
        write_json(summary_path, summary)
        next_action = summary.get("next_action", {})
        receipt_result: dict[str, Any] | None = None
        if (
            next_action.get("type") == "apply_main_actions_before_final_verification"
            and PHASES.index(phase) >= PHASES.index("draft_review")
        ):
            receipt_result = record_main_actions(
                task_dir,
                synthetic_markdown,
                summary_path=summary_path,
                replace=(artifact_dir / "main_action_receipt.json").exists(),
            )
            summary = collect_mas_run(task_dir, through_phase=phase)
            write_json(summary_path, summary)
            next_action = summary.get("next_action", {})
        phase_results.append(
            {
                "phase": phase,
                "task_files": grouped_task_files.get(phase, []),
                "emitted_artifacts": emitted_this_phase,
                "summary_file": str(summary_path),
                "collector_ok": bool(summary.get("ok")),
                "next_action": next_action,
                "main_action_receipt": receipt_result,
                "phase_gates": summary.get("phase_gates", []),
                "errors": summary.get("errors", []),
            }
        )
        if not summary.get("ok"):
            stop_reason = f"collector_not_ok:{phase}"
            break
        deferred_main_actions = (
            next_action.get("type") == "apply_main_actions_before_final_verification"
            and PHASES.index(phase) < PHASES.index("draft_review")
        )
        if (
            phase_index < len(PHASES) - 1
            and next_action.get("type") != "collect_or_dispatch_phase_artifacts"
            and not deferred_main_actions
        ):
            stop_reason = f"next_action_not_phase_dispatch:{next_action.get('type')}"
            break

    final_summary = collect_mas_run(task_dir)
    final_summary_path = task_dir / "mas_run_summary.json"
    write_json(final_summary_path, final_summary)
    combined_path = task_dir / "mas_artifacts_collected.json"

    collector_ok = all(bool(phase.get("collector_ok")) for phase in phase_results) and bool(final_summary.get("ok"))
    source_paths = [
        Path(str(item.get("path") or ""))
        for item in final_summary.get("artifact_sources", [])
        if isinstance(item, dict) and item.get("path")
    ]
    combined_artifacts, _, combined_errors, _ = merge_artifact_files(source_paths)
    errors.extend(combined_errors)
    combined_payload: dict[str, Any] = {"artifacts": combined_artifacts}
    if errors or not collector_ok:
        combined_payload.update(
            {
                "ok": False,
                "errors": errors + [str(error) for error in final_summary.get("errors", [])],
                "missing_artifacts": final_summary.get("missing_artifacts", []),
                "duplicate_artifacts": final_summary.get("duplicate_artifacts", []),
                "source_summary": {
                    "task_dir": str(task_dir),
                    "through_phase": final_summary.get("through_phase"),
                    "stop_reason": stop_reason,
                },
            }
        )
    write_json(combined_path, combined_payload)
    return {
        "schema_version": "1.0",
        "ok": not errors and collector_ok,
        "execution_mode": "deterministic_fixture_artifact_returns",
        "request_file": str(request_path),
        "artifact_fixture_file": str(artifact_fixture_path),
        "task_dir": str(task_dir),
        "bundle_file": dispatch_result["bundle_file"],
        "manifest_file": dispatch_result["manifest_file"],
        "artifact_dir": str(artifact_dir),
        "artifact_files": artifact_files,
        "phase_order": list(PHASES),
        "completed_phase_order": [str(phase["phase"]) for phase in phase_results],
        "stop_reason": stop_reason,
        "phases": phase_results,
        "final_summary_file": str(final_summary_path),
        "combined_artifacts_file": str(combined_path),
        "final_next_action": final_summary.get("next_action", {}),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a staged MAS dry run from synthetic specialist artifacts")
    parser.add_argument("--request-json", required=True, help="MAS task request JSON")
    parser.add_argument("--artifact-fixture", required=True, help="Synthetic MAS artifacts JSON fixture")
    parser.add_argument("--task-dir", required=True, help="Output dispatch/dry-run directory")
    parser.add_argument("--out", help="Write dry-run trace JSON")
    parser.add_argument("--overwrite", action="store_true", help="Remove an existing task-dir before writing")
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args()

    try:
        task_dir = Path(args.task_dir).expanduser()
        if task_dir.exists() and any(task_dir.iterdir()):
            if not args.overwrite:
                raise ValueError(f"task-dir is not empty; pass --overwrite to replace it: {task_dir}")
            if not can_overwrite_task_dir(task_dir):
                raise ValueError(
                    "refusing to overwrite task-dir without MAS dry-run marker or prior MAS temp outputs: "
                    f"{task_dir}"
                )
            shutil.rmtree(task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / DRY_RUN_MARKER).write_text("mas dry-run workspace\n", encoding="utf-8")

        result = run_mas_dry_run(Path(args.request_json), Path(args.artifact_fixture), task_dir)
    except Exception as exc:
        result = {
            "schema_version": "1.0",
            "ok": False,
            "execution_mode": "deterministic_fixture_artifact_returns",
            "errors": [f"MAS dry-run failed: {exc.__class__.__name__}: {exc}"],
        }
    if args.out:
        write_json(Path(args.out), result)
    if args.json or not args.out:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
