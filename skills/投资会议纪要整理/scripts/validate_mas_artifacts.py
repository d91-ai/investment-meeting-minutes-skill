#!/usr/bin/env python3
"""Validate MAS process artifacts for the meeting-minutes workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

REQUIRED_FIELDS: dict[str, list[str]] = {
    "source_manifest": [
        "source_mode",
        "materials",
        "archive_allowed",
        "archive_status",
        "skipped_reason",
    ],
    "transcript_audit": [
        "asr_primary",
        "asr_auxiliary",
        "quality_flags",
        "speaker_boundary_findings",
        "timestamp_index_status",
        "conflicts",
        "recommended_action",
    ],
    "source_reconciliation": [
        "primary_body_source",
        "primary_source_reason",
        "cross_check_source",
        "coverage_findings",
        "speaker_order_findings",
        "omission_findings",
        "conflicts",
        "manual_review_required",
    ],
    "entity_verification_report": [
        "items",
        "local_candidate_paths",
        "external_evidence_paths",
        "confirmed_item_evidence_paths",
        "confirmed_items",
        "unresolved_items",
        "conflicts",
    ],
    "target_attribution_review": [
        "segments_reviewed",
        "wrong_grouping",
        "missing_positive_targets",
        "incidental_targets_in_heading",
        "negative_targets_in_heading",
        "non_source_companies",
        "recommended_revisions",
    ],
    "fidelity_review": [
        "paragraphs_reviewed",
        "source_mapping_failures",
        "summary_compression_findings",
        "pronoun_rewrite_findings",
        "omission_findings",
        "recommended_revisions",
    ],
    "export_manifest": [
        "markdown_path",
        "markdown_sha256",
        "verification_sidecar_path",
        "validators_run",
        "regression_result",
        "export_status",
        "known_unverified_parts",
        "main_actions_verified",
    ],
    "main_action_receipt": [
        "run_id",
        "actions",
        "status",
        "markdown_path",
        "markdown_sha256",
        "source_artifact_digest",
    ],
}

DOUBTFUL_REQUIRED_FIELDS = [
    "原始表述",
    "存疑类型",
    "当前判断",
    "候选项",
    "是否需要 sidecar",
    "上下文依据",
    "检索/证据路径",
    "最终处理",
]
ALLOWED_DOUBTFUL_TYPES = {"人名", "说话人身份", "公司或证券标的", "行业术语", "数字或时间", "其他业务事实"}
FORBIDDEN_FINAL_FIELDS = {"final_markdown", "markdown_body", "final_note", "final_body"}
BOOLEAN_FIELD_RULES: dict[str, list[str]] = {
    "source_manifest": ["archive_allowed"],
    "source_reconciliation": ["manual_review_required"],
    "export_manifest": ["main_actions_verified"],
}
LIST_FIELD_RULES: dict[str, list[str]] = {
    "source_manifest": ["materials"],
    "transcript_audit": ["quality_flags", "speaker_boundary_findings", "conflicts"],
    "source_reconciliation": [
        "coverage_findings",
        "speaker_order_findings",
        "omission_findings",
        "conflicts",
    ],
    "entity_verification_report": [
        "items",
        "local_candidate_paths",
        "external_evidence_paths",
        "confirmed_items",
        "unresolved_items",
        "conflicts",
    ],
    "target_attribution_review": [
        "wrong_grouping",
        "missing_positive_targets",
        "incidental_targets_in_heading",
        "negative_targets_in_heading",
        "non_source_companies",
        "recommended_revisions",
    ],
    "fidelity_review": [
        "source_mapping_failures",
        "summary_compression_findings",
        "pronoun_rewrite_findings",
        "omission_findings",
        "recommended_revisions",
    ],
    "export_manifest": ["validators_run", "known_unverified_parts"],
    "main_action_receipt": ["actions"],
}
STRING_FIELD_RULES: dict[str, list[str]] = {
    "source_manifest": ["source_mode", "archive_status", "skipped_reason"],
    "transcript_audit": ["asr_primary", "asr_auxiliary", "timestamp_index_status", "recommended_action"],
    "source_reconciliation": ["primary_body_source", "primary_source_reason", "cross_check_source"],
    "export_manifest": [
        "markdown_path",
        "markdown_sha256",
        "verification_sidecar_path",
        "export_status",
    ],
    "main_action_receipt": [
        "run_id",
        "status",
        "markdown_path",
        "markdown_sha256",
        "source_artifact_digest",
    ],
}
TRANSCRIPT_ACTIONS = {"continue", "repair_transcript", "request_user"}
EXPORT_STATUSES = {"passed", "failed", "blocked"}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
EXTERNAL_SOURCE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{1,120}$")
MAIN_OWNED_ARTIFACTS = {"source_manifest", "main_action_receipt"}


def has_items(value: Any) -> bool:
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, dict):
        return bool(value)
    return value not in (None, "")


def as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def canonical_json_digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_set_digest(artifacts: dict[str, Any]) -> str:
    stable = {
        key: value
        for key, value in artifacts.items()
        if key not in {"export_manifest", "main_action_receipt"}
    }
    return canonical_json_digest(stable)


def artifact_mapping(payload: Any) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(payload, dict):
        return {}, ["MAS artifact 文件顶层必须是 JSON object"]
    if isinstance(payload.get("artifacts"), dict):
        return dict(payload["artifacts"]), []
    artifact_type = payload.get("artifact_type")
    if isinstance(artifact_type, str):
        if "artifact" in payload:
            return {artifact_type: payload["artifact"]}, []
        return {artifact_type: {key: value for key, value in payload.items() if key != "artifact_type"}}, []
    return {}, ["MAS artifact 文件必须包含 artifacts object 或 artifact_type"]


def validate_dispatch_identity(
    payload: Any,
    bundle: dict[str, Any],
    manifest: dict[str, Any],
    through_phase: str | None = None,
    phase_order: dict[str, int] | None = None,
    allow_internal_split: bool = False,
) -> list[str]:
    if not isinstance(payload, dict):
        return ["MAS artifact identity 顶层必须是 JSON object"]
    artifacts, mapping_errors = artifact_mapping(payload)
    errors = list(mapping_errors)
    artifact_types = set(artifacts)
    reserved_fields = {"task_artifact_set", "ingested_split"} & set(payload)
    if reserved_fields:
        errors.append(
            "MAS artifact 不得由返回方设置内部拆分字段: " + ", ".join(sorted(reserved_fields))
        )
    run_id = str(payload.get("run_id") or "")
    bundle_run_id = str(bundle.get("run_id") or "")
    manifest_run_id = str(manifest.get("run_id") or "")
    expected_run_id = bundle_run_id or manifest_run_id
    if not bundle_run_id:
        errors.append("MAS task bundle 缺少 run_id")
    if not manifest_run_id:
        errors.append("MAS dispatch manifest 缺少 run_id")
    if bundle_run_id and manifest_run_id and bundle_run_id != manifest_run_id:
        errors.append(
            "MAS bundle/manifest run_id 不一致: "
            f"bundle={bundle_run_id} manifest={manifest_run_id}"
        )
    if not expected_run_id:
        errors.append("MAS dispatch bundle 缺少 run_id")
    elif run_id != expected_run_id:
        errors.append(f"MAS artifact run_id 不匹配: expected={expected_run_id} actual={run_id}")

    task_id = str(payload.get("task_id") or "")
    dispatch_phase = str(payload.get("dispatch_phase") or "")
    owner = str(payload.get("artifact_owner") or "")
    if artifact_types and artifact_types <= MAIN_OWNED_ARTIFACTS:
        if owner != "Main Orchestrator":
            errors.append("Main-owned MAS artifact 的 artifact_owner 必须为 Main Orchestrator")
        expected_task_ids = {f"{expected_run_id}:main:{artifact_type}" for artifact_type in artifact_types}
        if len(expected_task_ids) != 1 or task_id not in expected_task_ids:
            errors.append("Main-owned MAS artifact task_id 与当前 run/artifact 不匹配")
        if dispatch_phase not in {"pre_draft", "draft_review"}:
            errors.append("Main-owned MAS artifact dispatch_phase 不合法")
        if through_phase and phase_order:
            if dispatch_phase not in phase_order or through_phase not in phase_order:
                errors.append("Main-owned MAS artifact phase 无法与 through_phase 比较")
            elif phase_order[dispatch_phase] > phase_order[through_phase]:
                errors.append(
                    "Main-owned MAS artifact 尚未到可接收 phase: "
                    f"artifact={dispatch_phase} through_phase={through_phase}"
                )
        return errors

    task_files = manifest.get("task_files")
    if not isinstance(task_files, list):
        return errors + ["MAS dispatch manifest task_files 必须是 JSON array"]
    matched = [item for item in task_files if isinstance(item, dict) and str(item.get("task_id") or "") == task_id]
    if len(matched) != 1:
        return errors + [f"MAS artifact task_id 未唯一匹配当前 dispatch task: {task_id}"]
    task = matched[0]
    task_run_id = str(task.get("run_id") or "")
    if task_run_id != expected_run_id:
        errors.append(
            "MAS dispatch task run_id 不匹配: "
            f"expected={expected_run_id} actual={task_run_id}"
        )
    expected_phase = str(task.get("dispatch_phase") or "")
    expected_owner = str(task.get("artifact_owner") or task.get("role") or "")
    expected_types = {str(task.get("artifact_type") or "")}
    expected_types.update(str(item) for item in task.get("secondary_artifacts", []))
    permission_types = expected_types if allow_internal_split else artifact_types
    if not allow_internal_split and permission_types != expected_types:
        errors.append(
            "MAS artifact 返回类型不符合 task 权限: "
            f"expected={sorted(expected_types)} actual={sorted(permission_types)}"
        )
    if allow_internal_split and not artifact_types <= expected_types:
        errors.append(
            "MAS artifact 文件包含 task 权限之外的 artifact: "
            f"expected={sorted(expected_types)} actual={sorted(artifact_types)}"
        )
    if dispatch_phase != expected_phase:
        errors.append(f"MAS artifact dispatch_phase 不匹配: expected={expected_phase} actual={dispatch_phase}")
    if owner != expected_owner:
        errors.append(f"MAS artifact artifact_owner 不匹配: expected={expected_owner} actual={owner}")
    if through_phase and phase_order:
        if expected_phase not in phase_order or through_phase not in phase_order:
            errors.append("MAS artifact phase 无法与 through_phase 比较")
        elif phase_order[expected_phase] > phase_order[through_phase]:
            errors.append(
                f"MAS artifact 尚未到可接收 phase: artifact={expected_phase} through_phase={through_phase}"
            )
    return errors


def validate_dispatch_context(bundle: Any, manifest: Any) -> list[str]:
    """Reject malformed control files before artifact-level collection."""
    errors: list[str] = []
    if not isinstance(bundle, dict):
        return ["MAS task bundle 顶层必须是 JSON object"]
    if not isinstance(manifest, dict):
        return ["MAS dispatch manifest 顶层必须是 JSON object"]

    bundle_run_id = str(bundle.get("run_id") or "")
    manifest_run_id = str(manifest.get("run_id") or "")
    if not bundle_run_id:
        errors.append("MAS task bundle 缺少 run_id")
    if not manifest_run_id:
        errors.append("MAS dispatch manifest 缺少 run_id")
    if bundle_run_id and manifest_run_id and bundle_run_id != manifest_run_id:
        errors.append("MAS bundle/manifest run_id 不一致")

    expected_artifacts = bundle.get("expected_artifacts")
    tasks = bundle.get("tasks")
    task_files = manifest.get("task_files")
    if not isinstance(expected_artifacts, list):
        errors.append("MAS task bundle expected_artifacts 必须是 JSON array")
        expected_artifacts = []
    if len(expected_artifacts) != len({str(item) for item in expected_artifacts}):
        errors.append("MAS task bundle expected_artifacts 不得包含重复项")
    if not isinstance(tasks, list):
        errors.append("MAS task bundle tasks 必须是 JSON array")
        tasks = []
    if not isinstance(task_files, list):
        errors.append("MAS dispatch manifest task_files 必须是 JSON array")
        task_files = []
    task_count = manifest.get("task_count")
    if not isinstance(task_count, int) or isinstance(task_count, bool):
        errors.append("MAS dispatch manifest task_count 必须是 integer")
    elif task_count != len(task_files):
        errors.append(
            "MAS dispatch manifest task_count 与 task_files 不一致: "
            f"declared={task_count} actual={len(task_files)}"
        )

    bundle_tasks = {
        str(task.get("task_id") or ""): task
        for task in tasks
        if isinstance(task, dict) and str(task.get("task_id") or "")
    }
    manifest_tasks = {
        str(task.get("task_id") or ""): task
        for task in task_files
        if isinstance(task, dict) and str(task.get("task_id") or "")
    }
    if len(bundle_tasks) != len(tasks):
        errors.append("MAS task bundle tasks 必须包含唯一非空 task_id")
    if len(manifest_tasks) != len(task_files):
        errors.append("MAS dispatch manifest task_files 必须包含唯一非空 task_id")
    if set(bundle_tasks) != set(manifest_tasks):
        errors.append("MAS bundle tasks 与 dispatch manifest task_files 的 task_id 集合不一致")

    expected_set = {str(item) for item in expected_artifacts}
    for task_id, task in bundle_tasks.items():
        manifest_task = manifest_tasks.get(task_id)
        if not isinstance(manifest_task, dict):
            continue
        task_artifact = str(task.get("artifact_type") or "")
        task_secondary = sorted(str(item) for item in task.get("secondary_artifacts", []))
        task_owner = str(task.get("artifact_owner") or task.get("role") or "")
        manifest_owner = str(manifest_task.get("artifact_owner") or manifest_task.get("role") or "")
        for field, left, right in [
            ("run_id", str(task.get("run_id") or ""), str(manifest_task.get("run_id") or "")),
            ("artifact_type", task_artifact, str(manifest_task.get("artifact_type") or "")),
            ("dispatch_phase", str(task.get("dispatch_phase") or ""), str(manifest_task.get("dispatch_phase") or "")),
            ("artifact_owner", task_owner, manifest_owner),
        ]:
            if left != right:
                errors.append(f"MAS bundle/manifest task {task_id} 的 {field} 不一致")
        manifest_secondary = sorted(str(item) for item in manifest_task.get("secondary_artifacts", []))
        if task_secondary != manifest_secondary:
            errors.append(f"MAS bundle/manifest task {task_id} 的 secondary_artifacts 不一致")
        if task_artifact and task_artifact not in expected_set:
            errors.append(f"MAS task artifact_type 不在 expected_artifacts 中: {task_artifact}")
        if any(item not in expected_set for item in task_secondary):
            errors.append(f"MAS task secondary_artifacts 不在 expected_artifacts 中: {task_id}")
    if bool(bundle.get("mas_required")) and not tasks:
        errors.append("MAS task bundle 启用 MAS 时必须包含 specialist tasks")
    if bool(bundle.get("mas_required")) != bool(manifest.get("mas_required")):
        errors.append("MAS bundle/manifest mas_required 不一致")
    return errors


def forbidden_field_errors(value: Any, path: str) -> list[str]:
    errors: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in FORBIDDEN_FINAL_FIELDS:
                errors.append(f"{child_path} 不得包含终稿字段: {key}")
            errors.extend(forbidden_field_errors(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(forbidden_field_errors(child, f"{path}[{index}]"))
    return errors


def validate_required_fields(artifact_type: str, artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return [f"{artifact_type} 必须是 JSON object"]
    missing = [field for field in REQUIRED_FIELDS[artifact_type] if field not in artifact]
    if missing:
        return [f"{artifact_type} 缺少字段: {', '.join(missing)}"]
    return []


def validate_field_types(artifact_type: str, artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    errors: list[str] = []
    for field in BOOLEAN_FIELD_RULES.get(artifact_type, []):
        if field in artifact and not isinstance(artifact[field], bool):
            errors.append(f"{artifact_type}.{field} 必须是 boolean")
    for field in LIST_FIELD_RULES.get(artifact_type, []):
        if field in artifact and not isinstance(artifact[field], list):
            errors.append(f"{artifact_type}.{field} 必须是 JSON array")
    for field in STRING_FIELD_RULES.get(artifact_type, []):
        if field in artifact and not isinstance(artifact[field], str):
            errors.append(f"{artifact_type}.{field} 必须是 string")
    return errors


def validate_transcript_audit(artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    action = artifact.get("recommended_action")
    if action not in TRANSCRIPT_ACTIONS:
        errors = [
            "transcript_audit.recommended_action 必须是以下之一: "
            + ", ".join(sorted(TRANSCRIPT_ACTIONS))
        ]
    else:
        errors = []
    if action == "continue" and (
        has_items(artifact.get("quality_flags"))
        or has_items(artifact.get("speaker_boundary_findings"))
        or has_items(artifact.get("conflicts"))
    ):
        errors.append("transcript_audit 存在质量问题或冲突时 recommended_action 不得为 continue")
    return errors


def validate_source_reconciliation(artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    if artifact.get("manual_review_required") is not False:
        return []
    errors: list[str] = []
    if not str(artifact.get("primary_body_source") or "").strip():
        errors.append("source_reconciliation 自动继续时 primary_body_source 不得为空")
    if not str(artifact.get("primary_source_reason") or "").strip():
        errors.append("source_reconciliation 自动继续时 primary_source_reason 不得为空")
    return errors


def validate_entity_verification_report(artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    errors: list[str] = []
    confirmed_items = [
        str(item).strip()
        for item in as_list(artifact.get("confirmed_items"))
        if str(item).strip()
    ]
    external_evidence_paths = {
        str(item).strip()
        for item in as_list(artifact.get("external_evidence_paths"))
        if str(item).strip()
    }
    local_candidate_paths = {
        str(item).strip()
        for item in as_list(artifact.get("local_candidate_paths"))
        if str(item).strip()
    }
    items = {
        str(item).strip()
        for item in as_list(artifact.get("items"))
        if str(item).strip()
    }
    unresolved_items = {
        str(item).strip()
        for item in as_list(artifact.get("unresolved_items"))
        if str(item).strip()
    }
    confirmed_set = set(confirmed_items)
    overlap = confirmed_set & unresolved_items
    if overlap:
        errors.append("entity_verification_report confirmed_items 与 unresolved_items 不得重叠: " + ", ".join(sorted(overlap)))
    unclassified = items - confirmed_set - unresolved_items
    unknown_classified = (confirmed_set | unresolved_items) - items
    if unclassified:
        errors.append("entity_verification_report items 存在未归类项: " + ", ".join(sorted(unclassified)))
    if unknown_classified:
        errors.append("entity_verification_report 归类项不在 items 中: " + ", ".join(sorted(unknown_classified)))
    reused_evidence = local_candidate_paths & external_evidence_paths
    if reused_evidence:
        errors.append(
            "entity_verification_report local_candidate_paths 与 external_evidence_paths 不得复用同一证据: "
            + ", ".join(sorted(reused_evidence))
        )
    for evidence in sorted(external_evidence_paths):
        is_url = evidence.startswith(("http://", "https://")) and " " not in evidence
        if not is_url and not EXTERNAL_SOURCE_ID.fullmatch(evidence):
            errors.append(
                "entity_verification_report external_evidence_paths 必须是 http(s) URL 或公开 source ID: "
                + evidence
            )
    if has_items(artifact.get("confirmed_items")) and not has_items(artifact.get("external_evidence_paths")):
        errors.append("entity_verification_report confirmed_items 非空时必须提供 external_evidence_paths")
    evidence_mapping = artifact.get("confirmed_item_evidence_paths")
    if confirmed_items and not isinstance(evidence_mapping, dict):
        errors.append("entity_verification_report confirmed_items 非空时必须提供 confirmed_item_evidence_paths 逐条证据映射")
    elif confirmed_items:
        for item in confirmed_items:
            evidence_values = [
                str(evidence).strip()
                for evidence in as_list(evidence_mapping.get(item))
                if str(evidence).strip()
            ]
            if not evidence_values:
                errors.append(f"entity_verification_report confirmed item 缺少逐条外部证据: {item}")
                continue
            for evidence in evidence_values:
                if evidence not in external_evidence_paths:
                    errors.append(
                        "entity_verification_report confirmed item 逐条证据必须来自 external_evidence_paths: "
                        f"{item} -> {evidence}"
                    )
    return errors


def validate_export_manifest(artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    errors: list[str] = []
    if not str(artifact.get("markdown_path") or "").strip():
        errors.append("export_manifest.markdown_path 不得为空")
    markdown_sha256 = str(artifact.get("markdown_sha256") or "").strip().lower()
    if not HEX_SHA256.fullmatch(markdown_sha256):
        errors.append("export_manifest.markdown_sha256 必须是 64 位小写 SHA-256")
    validators = artifact.get("validators_run")
    if not isinstance(validators, list) or not validators:
        errors.append("export_manifest.validators_run 必须是非空 JSON array")
    elif any(
        not isinstance(item, dict)
        or not str(item.get("name") or "").strip()
        or not isinstance(item.get("ok"), bool)
        for item in validators
    ):
        errors.append("export_manifest.validators_run 每项必须包含非空 name 和 boolean ok")
    regression = artifact.get("regression_result")
    if not isinstance(regression, dict) or not isinstance(regression.get("ok"), bool):
        errors.append("export_manifest.regression_result 必须包含 boolean ok")
    if artifact.get("export_status") not in EXPORT_STATUSES:
        errors.append("export_manifest.export_status 必须是以下之一: " + ", ".join(sorted(EXPORT_STATUSES)))
    return errors


def validate_main_action_receipt(artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    errors: list[str] = []
    for field in ("run_id", "markdown_path"):
        if not str(artifact.get(field) or "").strip():
            errors.append(f"main_action_receipt.{field} 不得为空")
    if artifact.get("status") != "applied":
        errors.append("main_action_receipt.status 必须为 applied")
    for field in ("markdown_sha256", "source_artifact_digest"):
        value = str(artifact.get(field) or "").strip().lower()
        if not HEX_SHA256.fullmatch(value):
            errors.append(f"main_action_receipt.{field} 必须是 64 位小写 SHA-256")
    actions = artifact.get("actions")
    if not isinstance(actions, list) or not actions or any(not str(action).strip() for action in actions):
        errors.append("main_action_receipt.actions 必须是非空 string array")
    return errors


def validate_doubtful_items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["doubtful_items 必须是 JSON array"]
    errors: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"doubtful_items 第 {index + 1} 条必须是 JSON object")
            continue
        missing = [field for field in DOUBTFUL_REQUIRED_FIELDS if field not in item]
        if missing:
            errors.append(f"doubtful_items 第 {index + 1} 条缺少字段: {', '.join(missing)}")
        doubtful_type = item.get("存疑类型")
        if doubtful_type not in ALLOWED_DOUBTFUL_TYPES:
            allowed = ", ".join(sorted(ALLOWED_DOUBTFUL_TYPES))
            errors.append(f"doubtful_items 第 {index + 1} 条存疑类型必须为固定枚举值: {allowed}")
    return errors


def validate_payload(payload: Any, required_artifacts: list[str] | None = None) -> dict[str, Any]:
    artifacts, errors = artifact_mapping(payload)
    warnings: list[str] = []
    required_artifacts = required_artifacts or []

    for artifact_type in required_artifacts:
        if artifact_type not in artifacts:
            errors.append(f"缺少必需 artifact: {artifact_type}")

    for artifact_type, artifact in artifacts.items():
        path = f"artifacts.{artifact_type}"
        if artifact_type == "doubtful_items":
            errors.extend(validate_doubtful_items(artifact))
        elif artifact_type in REQUIRED_FIELDS:
            errors.extend(validate_required_fields(artifact_type, artifact))
            errors.extend(validate_field_types(artifact_type, artifact))
            if artifact_type == "transcript_audit":
                errors.extend(validate_transcript_audit(artifact))
            elif artifact_type == "source_reconciliation":
                errors.extend(validate_source_reconciliation(artifact))
            elif artifact_type == "entity_verification_report":
                errors.extend(validate_entity_verification_report(artifact))
            elif artifact_type == "export_manifest":
                errors.extend(validate_export_manifest(artifact))
            elif artifact_type == "main_action_receipt":
                errors.extend(validate_main_action_receipt(artifact))
        else:
            errors.append(f"未知 MAS artifact 类型: {artifact_type}")
        errors.extend(forbidden_field_errors(artifact, path))

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "artifact_count": len(artifacts),
        "artifact_types": sorted(artifacts),
    }


def validate_file(path: Path, required_artifacts: list[str] | None = None) -> dict[str, Any]:
    return validate_payload(read_json(path), required_artifacts=required_artifacts)


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 MAS 过程 artifact JSON 字段完整性")
    parser.add_argument("artifact_file", help="MAS artifact JSON 文件")
    parser.add_argument("--require-artifact", action="append", default=[], help="要求存在的 artifact 类型，可重复")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    try:
        result = validate_file(Path(args.artifact_file), required_artifacts=[str(item) for item in args.require_artifact])
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        result = {
            "ok": False,
            "errors": [f"MAS artifact 文件无法读取或解析: {exc}"],
            "warnings": [],
            "artifact_count": 0,
            "artifact_types": [],
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "OK" if result["ok"] else "FAIL"
        print(f"[{status}] {args.artifact_file}")
        for warning in result["warnings"]:
            print(f"  warning: {warning}")
        for error in result["errors"]:
            print(f"  error: {error}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
