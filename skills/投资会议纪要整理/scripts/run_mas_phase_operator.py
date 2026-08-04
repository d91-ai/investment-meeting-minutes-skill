#!/usr/bin/env python3
"""Run one repeatable MAS operator loop without dispatching subagents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any

from build_mas_task_bundle import (
    _write_dispatch_files_unlocked,
    build_bundle_from_request,
    read_json,
    validate_bundle,
    write_dispatch_files,
)
from assemble_speaker_turn_edits import assemble_speaker_turn_edits
from assemble_entity_verification_shards import assemble_entity_verification_shards
from assemble_fidelity_review_shards import assemble_fidelity_review_shards
from collect_mas_artifacts import PHASE_ORDER, collect_mas_snapshot_unlocked
from create_mas_source_manifest import create_source_manifest, source_manifest_artifact
from ingest_mas_artifact import ingest_mas_artifact_file
from mas_task_lock import mas_task_lock
from mas_performance_telemetry import SCHEMA_VERSION as TELEMETRY_SCHEMA_VERSION, append_event
from plan_mas_next_action import plan_from_summary

DEFAULT_MAX_PARALLEL = 3
AUTO_ASSEMBLY_LIMIT = 3


def telemetry_profile(task_dir: Path, sample_kind: str) -> dict[str, Any]:
    bundle = read_json(task_dir / "mas_task_bundle.json")
    if not isinstance(bundle, dict):
        raise ValueError("telemetry requires a valid bound MAS bundle")
    meeting_map = {
        "专家交流": "expert_call",
        "专家访谈": "expert_call",
        "上市公司交流": "listed_company",
        "多人复盘会": "group_review",
    }
    speaker = bundle.get("speaker_editing") if isinstance(bundle.get("speaker_editing"), dict) else {}
    source_chars = int(speaker.get("source_char_count") or 0)
    entity = bundle.get("entity_candidate_manifest") if isinstance(bundle.get("entity_candidate_manifest"), dict) else {}
    candidate_count = int(entity.get("candidate_count") or 0)
    size_measure = max(source_chars, candidate_count * 256)
    size_profile = "small" if size_measure <= 16000 else "medium" if size_measure <= 64000 else "large"
    risks = bundle.get("risk_flags") if isinstance(bundle.get("risk_flags"), list) else []
    risk_profile = "low" if not risks else "medium" if len(risks) <= 3 else "high"
    effective_editing = str(speaker.get("effective_mode") or "direct")
    editing_mode = {
        "skip": "direct",
        "direct": "direct",
        "full": "full",
    }.get(effective_editing, "not_applicable")
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "source_mode": str(bundle.get("source_mode") or ""),
        "meeting_type": meeting_map.get(str(bundle.get("meeting_type") or ""), "other"),
        "size_profile": size_profile,
        "risk_profile": risk_profile,
        "editing_mode": editing_mode,
        "sample_kind": sample_kind,
        "candidate_count": candidate_count,
        "group_count": int(entity.get("group_count") or 0),
        "shard_count": int(entity.get("shard_count") or 0),
        "retry_count": 0,
    }


def record_operator_telemetry(
    path: Path | None,
    profile: dict[str, Any] | None,
    *,
    event_type: str,
    phase: str,
    task_kind: str,
    duration_ms: float,
    queue_ms: float = 0.0,
) -> str | None:
    if path is None or profile is None:
        return None
    try:
        append_event(
            path,
            {
                **profile,
                "event_type": event_type,
                "phase": phase,
                "task_kind": task_kind,
                "duration_ms": round(max(0.0, duration_ms), 3),
                "queue_ms": round(max(0.0, queue_ms), 3),
            },
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return f"telemetry record failed: {exc.__class__.__name__}: {exc}"
    return None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def default_path(task_dir: Path, explicit: str | None, filename: str) -> Path:
    return Path(explicit) if explicit else task_dir / filename


def prepare_dispatch(task_dir: Path, request_path: Path | None, overwrite_dispatch: bool) -> dict[str, Any]:
    task_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = task_dir / "mas_task_bundle.json"
    if request_path is None:
        if not bundle_path.exists():
            return {
                "created": False,
                "errors": [f"missing MAS task bundle: {bundle_path}"],
                "warnings": [],
            }
        return {"created": False, "errors": [], "warnings": [], "bundle_file": str(bundle_path)}

    if bundle_path.exists() and not overwrite_dispatch:
        return {
            "created": False,
            "errors": [
                f"task_dir already has mas_task_bundle.json; omit --request-json or pass --overwrite-dispatch: {task_dir}"
            ],
            "warnings": [],
            "bundle_file": str(bundle_path),
        }

    try:
        request = read_json(request_path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            "created": False,
            "errors": [f"request-json cannot be read or parsed: {request_path}: {exc}"],
            "warnings": [],
        }
    if not isinstance(request, dict):
        return {"created": False, "errors": [f"request-json must be a JSON object: {request_path}"], "warnings": []}
    bundle = build_bundle_from_request(request)
    errors = validate_bundle(bundle)
    if errors:
        return {"created": False, "errors": errors, "warnings": []}
    try:
        dispatch_files = write_dispatch_files(bundle, task_dir, overwrite_prompts=overwrite_dispatch)
    except (OSError, ValueError) as exc:
        return {"created": False, "errors": [f"dispatch files cannot be written: {exc}"], "warnings": []}
    return {"created": True, "errors": [], "warnings": [], **dispatch_files}


def _auto_write_source_manifest_unlocked(task_dir: Path, request_path: Path | None) -> dict[str, Any]:
    artifact_path = task_dir / "artifacts" / "source_manifest.json"
    if artifact_path.exists():
        return {
            "enabled": True,
            "status": "already_exists",
            "artifact_file": str(artifact_path),
            "errors": [],
            "warnings": [],
        }
    # Always use the bound dispatch bundle. A raw request has no run_id and is
    # not the authoritative post-dispatch context.
    context_path = task_dir / "mas_task_bundle.json"
    context = read_json(context_path)
    if not isinstance(context, dict):
        return {
            "enabled": True,
            "status": "failed",
            "artifact_file": "",
            "errors": [f"source_manifest context must be a JSON object: {context_path}"],
            "warnings": [],
        }
    manifest, warnings = create_source_manifest(context)
    run_id = str(context.get("run_id") or "")
    if not run_id:
        raise ValueError("source_manifest context missing dispatch run_id")
    write_json(artifact_path, source_manifest_artifact(manifest, run_id))
    return {
        "enabled": True,
        "status": "written",
        "artifact_file": str(artifact_path),
        "errors": [],
        "warnings": warnings,
    }


def auto_write_source_manifest(task_dir: Path, request_path: Path | None) -> dict[str, Any]:
    with mas_task_lock(task_dir, exclusive=True):
        return _auto_write_source_manifest_unlocked(task_dir, request_path)


def initialize_dispatch_atomic(
    task_dir: Path,
    request_path: Path,
    *,
    overwrite_dispatch: bool,
    through_phase: str | None,
    max_parallel: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create dispatch, source manifest, initial collector snapshot, and plan under one lock."""

    task_dir.mkdir(parents=True, exist_ok=True)
    with mas_task_lock(task_dir, exclusive=True):
        request = read_json(request_path)
        if not isinstance(request, dict):
            raise ValueError(f"request-json must be a JSON object: {request_path}")
        bundle = build_bundle_from_request(request)
        bundle_errors = validate_bundle(bundle)
        if bundle_errors:
            raise ValueError("invalid MAS request bundle: " + "; ".join(bundle_errors))
        dispatch = _write_dispatch_files_unlocked(
            bundle,
            task_dir,
            overwrite_prompts=overwrite_dispatch,
        )
        source_result = _auto_write_source_manifest_unlocked(task_dir, request_path)
        summary, combined_payload, combined_errors = collect_mas_snapshot_unlocked(
            task_dir,
            through_phase=through_phase,
        )
        plan = plan_from_summary(summary, max_parallel=max_parallel)
        write_json(task_dir / "mas_run_summary.json", summary)
        write_json(task_dir / "mas_artifacts_collected.json", combined_payload)
        write_json(task_dir / "mas_next_action_plan.json", plan)
        return (
            {
                "created": True,
                "atomic_init": True,
                "source_snapshot_locked": True,
                "initial_plan_written": True,
                "errors": [],
                "warnings": [str(item) for item in combined_errors],
                **dispatch,
            },
            source_result,
        )


def operator_status(plan: dict[str, Any], ingest_results: list[dict[str, Any]]) -> str:
    if any(not bool(result.get("ok")) for result in ingest_results):
        return "repair_return_artifacts"
    if not bool(plan.get("ok")):
        return "inspect_plan"
    plan_status = str(plan.get("plan_status") or "")
    if plan_status == "repair_before_continue":
        return "repair_before_continue"
    if plan_status == "dispatch_or_collect_phase":
        has_dispatch = bool(plan.get("dispatch_tasks"))
        has_main_owned = bool(plan.get("main_owned_missing_artifacts"))
        if has_dispatch and has_main_owned:
            return "prepare_main_owned_and_dispatch_subagents"
        if has_main_owned:
            return "create_main_owned_artifacts"
        if has_dispatch:
            return "dispatch_subagent_tasks"
        return "collect_phase_artifacts"
    if plan_status == "assemble_speaker_turns":
        return "assemble_speaker_turns"
    if plan_status == "assemble_entity_verification":
        return "assemble_entity_verification"
    if plan_status == "assemble_fidelity_review":
        return "assemble_fidelity_review"
    if plan_status == "ask_user":
        return "ask_user"
    if plan_status == "apply_main_actions":
        return "apply_main_actions"
    if plan_status == "continue":
        return "continue_main_workflow"
    return "inspect_summary"


def stop_reason_for(status: str) -> str:
    reasons = {
        "repair_return_artifacts": "invalid_or_duplicate_return_artifacts_need_repair",
        "inspect_plan": "next_action_plan_failed",
        "repair_before_continue": "collector_requires_artifact_repair_before_continue",
        "prepare_main_owned_and_dispatch_subagents": "waiting_for_main_owned_artifacts_and_subagent_returns",
        "create_main_owned_artifacts": "waiting_for_main_owned_artifacts",
        "dispatch_subagent_tasks": "waiting_for_subagent_returns",
        "collect_phase_artifacts": "waiting_for_phase_artifact_collection",
        "assemble_speaker_turns": "main_workflow_must_assemble_speaker_turns",
        "assemble_entity_verification": "main_workflow_must_assemble_entity_verification",
        "assemble_fidelity_review": "main_workflow_must_assemble_fidelity_review",
        "ask_user": "user_confirmation_required",
        "apply_main_actions": "main_workflow_must_apply_actions",
        "continue_main_workflow": "continue_without_user_intervention",
    }
    return reasons.get(status, "inspect_operator_state")


def run_mas_phase_operator(
    task_dir: Path,
    request_path: Path | None = None,
    return_paths: list[Path] | None = None,
    through_phase: str | None = None,
    summary_out: Path | None = None,
    combined_out: Path | None = None,
    plan_out: Path | None = None,
    state_out: Path | None = None,
    overwrite_dispatch: bool = False,
    auto_source_manifest: bool = False,
    replace_existing: bool = False,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    initialize: bool = False,
    auto_assemble: bool = True,
    telemetry_path: Path | None = None,
    telemetry_sample_kind: str = "production",
) -> dict[str, Any]:
    operator_started = time.perf_counter()
    task_dir = task_dir.expanduser()
    return_paths = return_paths or []
    errors: list[str] = []
    warnings: list[str] = []

    if initialize and request_path is None:
        raise ValueError("--init requires --request-json")
    source_manifest_result = {"enabled": False, "status": "not_requested", "errors": [], "warnings": []}
    if initialize:
        try:
            dispatch, source_manifest_result = initialize_dispatch_atomic(
                task_dir,
                request_path,
                overwrite_dispatch=overwrite_dispatch,
                through_phase=through_phase,
                max_parallel=max_parallel,
            )
        except Exception as exc:
            dispatch = {
                "created": False,
                "errors": [f"atomic init failed: {exc.__class__.__name__}: {exc}"],
                "warnings": [],
            }
    else:
        dispatch = prepare_dispatch(task_dir, request_path, overwrite_dispatch)
    errors.extend(str(error) for error in dispatch.get("errors", []))
    warnings.extend(str(warning) for warning in dispatch.get("warnings", []))

    if auto_source_manifest and not initialize and not errors:
        try:
            source_manifest_result = auto_write_source_manifest(task_dir, request_path)
        except Exception as exc:
            source_manifest_result = {
                "enabled": True,
                "status": "failed",
                "artifact_file": "",
                "errors": [f"auto source_manifest failed: {exc.__class__.__name__}: {exc}"],
                "warnings": [],
            }
        errors.extend(str(error) for error in source_manifest_result.get("errors", []))
        warnings.extend(str(warning) for warning in source_manifest_result.get("warnings", []))

    telemetry: dict[str, Any] | None = None
    if telemetry_path is not None and not errors:
        try:
            telemetry = telemetry_profile(task_dir, telemetry_sample_kind)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            warnings.append(f"telemetry profile failed: {exc.__class__.__name__}: {exc}")
        telemetry_error = record_operator_telemetry(
            telemetry_path,
            telemetry,
            event_type="phase_start",
            phase=through_phase or "pre_draft",
            task_kind="operator",
            duration_ms=(time.perf_counter() - operator_started) * 1000,
        )
        if telemetry_error:
            warnings.append(telemetry_error)

    ingest_results: list[dict[str, Any]] = []
    if not errors:
        for return_path in return_paths:
            ingest_started = time.perf_counter()
            result = ingest_mas_artifact_file(
                return_path,
                task_dir,
                through_phase=through_phase,
                replace_existing=replace_existing,
            )
            ingest_results.append(result)
            warnings.extend(str(warning) for warning in result.get("warnings", []))
            telemetry_error = record_operator_telemetry(
                telemetry_path,
                telemetry,
                event_type="ingest",
                phase=through_phase or "pre_draft",
                task_kind="specialist_return",
                duration_ms=(time.perf_counter() - ingest_started) * 1000,
            )
            if telemetry_error:
                warnings.append(telemetry_error)

    summary: dict[str, Any] = {}
    plan: dict[str, Any] = {}
    combined_errors: list[str] = []
    summary_path = default_path(task_dir, str(summary_out) if summary_out else None, "mas_run_summary.json")
    combined_path = default_path(
        task_dir,
        str(combined_out) if combined_out else None,
        "mas_artifacts_collected.json",
    )
    plan_path = default_path(task_dir, str(plan_out) if plan_out else None, "mas_next_action_plan.json")
    state_path = default_path(task_dir, str(state_out) if state_out else None, "mas_operator_state.json")

    with mas_task_lock(task_dir, exclusive=True):
        if not errors:
            summary, combined_payload, combined_errors = collect_mas_snapshot_unlocked(
                task_dir,
                through_phase=through_phase,
            )
            write_json(summary_path, summary)
            write_json(combined_path, combined_payload)
            plan = plan_from_summary(summary, max_parallel=max_parallel)
            write_json(plan_path, plan)
            warnings.extend(combined_errors)

        status = operator_status(plan, ingest_results) if plan else "inspect_operator_state"
        result_errors = errors + [
            str(error)
            for item in ingest_results
            for error in item.get("errors", [])
            if not item.get("ok")
        ]
        command_ok = not result_errors and bool(plan.get("ok", False))
        gate_ok = bool(summary.get("ok")) if summary else False
        complete = gate_ok and status == "continue_main_workflow" and str(plan.get("phase") or "") == "complete"
        result = {
            "schema_version": "1.0",
            "ok": command_ok,
            "command_ok": command_ok,
            "gate_ok": gate_ok,
            "complete": complete,
            "execution_mode": "operator_harness_no_subagent_dispatch_no_final_markdown",
            "task_dir": str(task_dir),
            "through_phase": through_phase or "complete",
            "dispatch": dispatch,
            "auto_source_manifest": source_manifest_result,
            "ingested_return_count": len(return_paths),
            "ingest_results": ingest_results,
            "collector_ok": bool(summary.get("ok")) if summary else False,
            "collector_summary_file": str(summary_path),
            "combined_artifacts_file": str(combined_path),
            "next_action_plan_file": str(plan_path),
            "operator_state_file": str(state_path),
            "operator_status": status,
            "stop_reason": stop_reason_for(status),
            "plan_status": plan.get("plan_status") if plan else "",
            "next_action_type": plan.get("next_action_type") if plan else "",
            "phase": plan.get("phase") if plan else "",
            "dispatch_tasks": plan.get("dispatch_tasks", []) if plan else [],
            "dispatch_batches": plan.get("dispatch_batches", []) if plan else [],
            "dispatch_waves": plan.get("dispatch_waves", []) if plan else [],
            "max_parallel": max_parallel,
            "entity_max_parallel": plan.get("entity_max_parallel", max_parallel) if plan else max_parallel,
            "main_owned_missing_artifacts": plan.get("main_owned_missing_artifacts", []) if plan else [],
            "repair_errors": plan.get("repair_errors", []) if plan else [],
            "main_actions": plan.get("main_actions", []) if plan else [],
            "main_action_checklist": plan.get("main_action_checklist", []) if plan else [],
            "summary": summary,
            "plan": plan,
            "errors": result_errors,
            "warnings": warnings,
        }
        write_json(state_path, result)

    assembly_results: list[dict[str, Any]] = []
    if auto_assemble and result.get("ok"):
        assembly_actions = {
            "assemble_edited_turns_before_draft_review": (
                "speaker_editing",
                lambda: assemble_speaker_turn_edits(task_dir, replace=replace_existing),
            ),
            "assemble_entity_verification_before_draft": (
                "entity_verification",
                lambda: assemble_entity_verification_shards(task_dir, replace=replace_existing),
            ),
            "assemble_fidelity_review_before_main_actions": (
                "fidelity_review",
                lambda: assemble_fidelity_review_shards(task_dir, replace=replace_existing),
            ),
        }
        for _ in range(AUTO_ASSEMBLY_LIMIT):
            action_type = str(result.get("next_action_type") or "")
            action = assembly_actions.get(action_type)
            if action is None:
                break
            assembly_kind, callback = action
            try:
                assembly_started = time.perf_counter()
                assembly_payload = callback()
                assembly_results.append({"assembly": assembly_kind, **assembly_payload})
                telemetry_error = record_operator_telemetry(
                    telemetry_path,
                    telemetry,
                    event_type="assembly",
                    phase=str(result.get("phase") or through_phase or "not_applicable"),
                    task_kind=assembly_kind,
                    duration_ms=(time.perf_counter() - assembly_started) * 1000,
                )
                if telemetry_error:
                    warnings.append(telemetry_error)
            except Exception as exc:
                assembly_results.append(
                    {
                        "assembly": assembly_kind,
                        "ok": False,
                        "errors": [f"automatic assembly failed: {exc.__class__.__name__}: {exc}"],
                    }
                )
                result["ok"] = False
                result["command_ok"] = False
                result["errors"] = list(result.get("errors", [])) + assembly_results[-1]["errors"]
                break
            refreshed = run_mas_phase_operator(
                task_dir=task_dir,
                request_path=None,
                return_paths=[],
                through_phase=through_phase,
                summary_out=summary_out,
                combined_out=combined_out,
                plan_out=plan_out,
                state_out=state_out,
                overwrite_dispatch=False,
                auto_source_manifest=False,
                replace_existing=replace_existing,
                max_parallel=max_parallel,
                initialize=False,
                auto_assemble=False,
                telemetry_path=None,
            )
            refreshed["dispatch"] = dispatch
            refreshed["auto_source_manifest"] = source_manifest_result
            refreshed["ingested_return_count"] = len(return_paths)
            refreshed["ingest_results"] = ingest_results
            refreshed["warnings"] = warnings + list(refreshed.get("warnings", []))
            result = refreshed
    result["assembly_results"] = assembly_results
    result["automatic_assembly_enabled"] = auto_assemble
    end_phase = "complete" if result.get("complete") else str(result.get("phase") or through_phase or "not_applicable")
    telemetry_error = record_operator_telemetry(
        telemetry_path,
        telemetry,
        event_type="phase_end",
        phase=end_phase,
        task_kind="operator",
        duration_ms=(time.perf_counter() - operator_started) * 1000,
    )
    if telemetry_error:
        result["warnings"] = list(result.get("warnings", [])) + [telemetry_error]
    result["performance_telemetry"] = {
        "enabled": telemetry_path is not None,
        "privacy_schema": TELEMETRY_SCHEMA_VERSION if telemetry_path is not None else "",
        "sample_kind": telemetry_sample_kind if telemetry_path is not None else "",
    }
    with mas_task_lock(task_dir, exclusive=True):
        write_json(state_path, result)
    return result


def load_return_batch(batch_path: Path) -> list[Path]:
    """Load an explicit JSON array of return paths; globs and artifact payloads are rejected."""

    payload = read_json(batch_path)
    if not isinstance(payload, list) or any(not isinstance(item, str) or not item.strip() for item in payload):
        raise ValueError(f"return batch must be a JSON array of non-empty path strings: {batch_path}")
    paths: list[Path] = []
    for raw in payload:
        if any(token in raw for token in ("*", "?", "[", "]")):
            raise ValueError(f"return batch paths must be explicit and cannot contain globs: {raw}")
        path = Path(raw)
        paths.append(path if path.is_absolute() else batch_path.parent / path)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one MAS operator loop over a dispatch directory")
    parser.add_argument("--task-dir", required=True, help="MAS dispatch directory")
    parser.add_argument("--request-json", help="Initialize the dispatch directory from a MAS request JSON")
    parser.add_argument("--return-json", action="append", default=[], help="Returned artifact JSON; may repeat")
    parser.add_argument(
        "--return-batch-json",
        action="append",
        default=[],
        help="JSON array of returned artifact paths; may repeat (best-effort, non-atomic ingest)",
    )
    parser.add_argument("--through-phase", choices=sorted(PHASE_ORDER), help="Collector phase gate to evaluate")
    parser.add_argument("--summary-out", help="Write collector summary JSON")
    parser.add_argument("--combined-out", help="Write combined artifacts JSON")
    parser.add_argument("--plan-out", help="Write next-action plan JSON")
    parser.add_argument("--state-out", help="Write operator state JSON")
    parser.add_argument("--overwrite-dispatch", action="store_true", help="Allow replacing an existing dispatch bundle")
    parser.add_argument("--replace-existing", action="store_true", help="Archive and replace same-task returned artifacts")
    parser.add_argument("--auto-source-manifest", action="store_true", help="Create source_manifest if missing")
    parser.add_argument(
        "--init",
        action="store_true",
        help="Atomically create bundle, dispatch prompts, source manifest, collector snapshot, and initial plan",
    )
    parser.add_argument(
        "--no-auto-assemble",
        action="store_true",
        help="Do not run eligible deterministic speaker/entity/fidelity assembly after ingest",
    )
    parser.add_argument("--telemetry-jsonl", help="Append privacy-safe anonymous timing events to one sample JSONL")
    parser.add_argument(
        "--telemetry-sample-kind",
        choices=["production", "synthetic", "non_production"],
        default="production",
    )
    parser.add_argument(
        "--max-parallel",
        type=int,
        default=DEFAULT_MAX_PARALLEL,
        help=f"speaker editing 并发 agent 槽位（1-8，默认 {DEFAULT_MAX_PARALLEL}）",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON; default is also JSON")
    args = parser.parse_args()

    try:
        return_paths = [Path(path) for path in args.return_json]
        for batch_json in args.return_batch_json:
            return_paths.extend(load_return_batch(Path(batch_json)))
        result = run_mas_phase_operator(
            task_dir=Path(args.task_dir),
            request_path=Path(args.request_json) if args.request_json else None,
            return_paths=return_paths,
            through_phase=args.through_phase,
            summary_out=Path(args.summary_out) if args.summary_out else None,
            combined_out=Path(args.combined_out) if args.combined_out else None,
            plan_out=Path(args.plan_out) if args.plan_out else None,
            state_out=Path(args.state_out) if args.state_out else None,
            overwrite_dispatch=bool(args.overwrite_dispatch),
            auto_source_manifest=bool(args.auto_source_manifest),
            replace_existing=bool(args.replace_existing),
            max_parallel=args.max_parallel,
            initialize=bool(args.init),
            auto_assemble=not bool(args.no_auto_assemble),
            telemetry_path=Path(args.telemetry_jsonl) if args.telemetry_jsonl else None,
            telemetry_sample_kind=args.telemetry_sample_kind,
        )
        result["batch_semantics"] = "best_effort_non_atomic" if args.return_batch_json else "sequential_explicit"
    except Exception as exc:
        result = {
            "schema_version": "1.0",
            "ok": False,
            "command_ok": False,
            "gate_ok": False,
            "complete": False,
            "execution_mode": "operator_harness_no_subagent_dispatch_no_final_markdown",
            "errors": [f"MAS phase operator failed: {exc.__class__.__name__}: {exc}"],
            "warnings": [],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
