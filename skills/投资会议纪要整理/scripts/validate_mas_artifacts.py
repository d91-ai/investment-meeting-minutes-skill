#!/usr/bin/env python3
"""Validate MAS process artifacts for the meeting-minutes workflow."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
        "reviewed_markdown_path",
        "reviewed_markdown_sha256",
        "segments_reviewed",
        "wrong_grouping",
        "missing_positive_targets",
        "incidental_targets_in_heading",
        "negative_targets_in_heading",
        "non_source_companies",
        "recommended_revisions",
    ],
    "fidelity_review": [
        "reviewed_markdown_path",
        "reviewed_markdown_sha256",
        "paragraphs_reviewed",
        "source_mapping_failures",
        "summary_compression_findings",
        "pronoun_rewrite_findings",
        "omission_findings",
        "recommended_revisions",
    ],
    "speaker_turn_edit": [
        "manifest_sha256",
        "shard_id",
        "speaker_ids",
        "input_sha256",
        "status",
        "edited_turns",
        "unresolved_spans",
    ],
    "editing_assembly_receipt": [
        "manifest_sha256",
        "edit_artifact_digest",
        "ordered_turn_ids",
        "assembled_draft_path",
        "assembled_draft_sha256",
        "status",
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
NON_BUSINESS_DOUBTFUL_TYPES = {"人名", "说话人身份"}
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
    "speaker_turn_edit": ["speaker_ids", "edited_turns", "unresolved_spans"],
    "editing_assembly_receipt": ["ordered_turn_ids"],
    "export_manifest": ["validators_run", "known_unverified_parts"],
    "main_action_receipt": ["actions"],
}
STRING_FIELD_RULES: dict[str, list[str]] = {
    "source_manifest": ["source_mode", "archive_status", "skipped_reason"],
    "transcript_audit": ["asr_primary", "asr_auxiliary", "timestamp_index_status", "recommended_action"],
    "source_reconciliation": ["primary_body_source", "primary_source_reason", "cross_check_source"],
    "target_attribution_review": ["reviewed_markdown_path", "reviewed_markdown_sha256"],
    "fidelity_review": ["reviewed_markdown_path", "reviewed_markdown_sha256"],
    "speaker_turn_edit": [
        "manifest_sha256",
        "shard_id",
        "input_sha256",
        "status",
    ],
    "editing_assembly_receipt": [
        "manifest_sha256",
        "edit_artifact_digest",
        "assembled_draft_path",
        "assembled_draft_sha256",
        "status",
    ],
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
OBJECT_FIELD_RULES: dict[str, list[str]] = {
    "entity_verification_report": ["confirmed_item_evidence_paths"],
    "export_manifest": ["regression_result"],
}
POSITIVE_INTEGER_FIELD_RULES: dict[str, list[str]] = {
    "target_attribution_review": ["segments_reviewed"],
    "fidelity_review": ["paragraphs_reviewed"],
}
TRANSCRIPT_ACTIONS = {"continue", "repair_transcript", "request_user"}
EXPORT_STATUSES = {"passed", "failed", "blocked"}
SOURCE_MODES = {"document_only", "audio_only", "audio_plus_document"}
ARCHIVE_STATUSES = {"not_started", "completed", "skipped", "skipped_for_fixture", "failed"}
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
PUBLIC_SOURCE_IDS = {
    "a_stock_data_live",
    "cninfo",
    "company_website",
    "exchange_disclosure",
    "professional_database",
    "regulatory_disclosure",
}
REQUIRED_VALIDATOR_NAMES = {"validate_utf8_text.py", "validate_meeting_minutes_contract.py"}
REGRESSION_VALIDATOR_NAME = "run_meeting_minutes_regression.py"
MAIN_OWNED_ARTIFACTS = {"source_manifest", "editing_assembly_receipt", "main_action_receipt"}
SPEAKER_TURN_EDIT_PREFIX = "speaker_turn_edit__"
SPEAKER_TURN_EDIT_STATUSES = {"complete", "blocked"}
SAFE_DYNAMIC_ARTIFACT = re.compile(r"^speaker_turn_edit__[a-z0-9_]+$")
SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "awsaccesskeyid",
    "bearer",
    "client_secret",
    "credential",
    "credentials",
    "key",
    "googleaccessid",
    "jwt",
    "password",
    "passwd",
    "private_key",
    "pwd",
    "secret",
    "session",
    "sessionid",
    "sig",
    "signature",
    "token",
}
SENSITIVE_QUERY_SUFFIXES = (
    "auth",
    "credential",
    "credentials",
    "jwt",
    "key",
    "password",
    "passwd",
    "secret",
    "session",
    "sig",
    "signature",
    "token",
)
SENSITIVE_QUERY_COMPACT_KEYS = {
    re.sub(r"[^a-z0-9]", "", key.lower())
    for key in SENSITIVE_QUERY_KEYS
} | {
    "awsaccesskeyid",
    "googleaccessid",
    "xgoogsignature",
}


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


def is_public_evidence_url(value: str) -> bool:
    if any(char.isspace() for char in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal", ".lan")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(hostname))
        except OSError:
            address = None
    if address is not None and any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    ):
        return False
    raw_query_keys = {key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    for raw_key in raw_query_keys:
        camel_split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw_key)
        normalized = re.sub(r"[^a-z0-9]+", "_", camel_split.lower()).strip("_")
        compact = re.sub(r"[^a-z0-9]", "", raw_key.lower())
        if (
            normalized in SENSITIVE_QUERY_KEYS
            or compact in SENSITIVE_QUERY_COMPACT_KEYS
            or normalized.startswith(("x_amz_", "x_goog_"))
            or normalized.endswith(tuple(f"_{suffix}" for suffix in SENSITIVE_QUERY_SUFFIXES))
        ):
            return False
    return True


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
        if key not in {"export_manifest", "editing_assembly_receipt", "main_action_receipt"}
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


def artifact_schema_name(artifact_type: str) -> str:
    if artifact_type.startswith(SPEAKER_TURN_EDIT_PREFIX):
        return "speaker_turn_edit"
    return artifact_type


def validate_speaker_edit_task_context(
    artifact_type: str,
    artifact: Any,
    task: dict[str, Any],
) -> list[str]:
    if artifact_schema_name(artifact_type) != "speaker_turn_edit":
        return []
    if not isinstance(artifact, dict):
        return [f"{artifact_type} 必须是 JSON object"]
    context = task.get("task_context")
    if not isinstance(context, dict):
        return [f"{artifact_type} dispatch task 缺少 task_context"]
    errors: list[str] = []
    for field in ("manifest_sha256", "shard_id", "speaker_ids", "input_sha256"):
        if artifact.get(field) != context.get(field):
            errors.append(f"{artifact_type}.{field} 与 dispatch task_context 不一致")
    expected_turns = {
        str(turn.get("turn_id") or ""): turn
        for turn in context.get("turns", [])
        if isinstance(turn, dict)
    }
    returned_turns = artifact.get("edited_turns")
    if not isinstance(returned_turns, list):
        return errors
    returned_by_id = {
        str(turn.get("turn_id") or ""): turn
        for turn in returned_turns
        if isinstance(turn, dict)
    }
    expected_turn_ids = list(expected_turns)
    returned_turn_ids = [
        str(turn.get("turn_id") or "")
        for turn in returned_turns
        if isinstance(turn, dict)
    ]
    if len(returned_by_id) != len(returned_turns):
        errors.append(f"{artifact_type}.edited_turns turn_id 必须唯一")
    if set(returned_by_id) != set(expected_turns):
        errors.append(f"{artifact_type}.edited_turns 必须恰好覆盖 dispatch task_context turns")
    elif returned_turn_ids != expected_turn_ids:
        errors.append(f"{artifact_type}.edited_turns 必须保持 dispatch task_context turns 顺序")
    for turn_id in sorted(set(returned_by_id) & set(expected_turns)):
        returned = returned_by_id[turn_id]
        expected = expected_turns[turn_id]
        for field in ("sequence", "speaker_id", "source_sha256"):
            if returned.get(field) != expected.get(field):
                errors.append(f"{artifact_type}.{turn_id}.{field} 与 dispatch task_context 不一致")
    return errors


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
        expected_phase_by_type = {
            "source_manifest": "pre_draft",
            "editing_assembly_receipt": "editing",
            "main_action_receipt": "draft_review",
        }
        expected_phases = {expected_phase_by_type[artifact_type] for artifact_type in artifact_types}
        if len(expected_phases) != 1 or dispatch_phase not in expected_phases:
            errors.append(
                "Main-owned MAS artifact dispatch_phase 不匹配: "
                f"expected={sorted(expected_phases)} actual={dispatch_phase}"
            )
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
    bundle_task_matches = [
        item
        for item in bundle.get("tasks", [])
        if isinstance(item, dict) and str(item.get("task_id") or "") == task_id
    ]
    context_task = bundle_task_matches[0] if len(bundle_task_matches) == 1 else task
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
    for artifact_type, artifact in artifacts.items():
        errors.extend(validate_speaker_edit_task_context(str(artifact_type), artifact, context_task))
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
            ("artifact_schema", str(task.get("artifact_schema") or ""), str(manifest_task.get("artifact_schema") or "")),
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
    for field in OBJECT_FIELD_RULES.get(artifact_type, []):
        if field in artifact and not isinstance(artifact[field], dict):
            errors.append(f"{artifact_type}.{field} 必须是 JSON object")
    for field in POSITIVE_INTEGER_FIELD_RULES.get(artifact_type, []):
        value = artifact.get(field)
        if field in artifact and (
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
        ):
            errors.append(f"{artifact_type}.{field} 必须是正整数")
    return errors


def validate_speaker_turn_edit(artifact_type: str, artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    errors: list[str] = []
    if not SAFE_DYNAMIC_ARTIFACT.fullmatch(artifact_type):
        errors.append(f"{artifact_type} 动态 artifact key 不安全")
    for field in ("manifest_sha256", "input_sha256"):
        value = str(artifact.get(field) or "")
        if not HEX_SHA256.fullmatch(value):
            errors.append(f"{artifact_type}.{field} 必须是小写 SHA-256")
    if artifact.get("status") not in SPEAKER_TURN_EDIT_STATUSES:
        errors.append(f"{artifact_type}.status 必须是 complete 或 blocked")
    speaker_ids = artifact.get("speaker_ids")
    if isinstance(speaker_ids, list):
        normalized_speaker_ids = [str(item) for item in speaker_ids]
        if (
            not normalized_speaker_ids
            or any(not item for item in normalized_speaker_ids)
            or len(normalized_speaker_ids) != len(set(normalized_speaker_ids))
        ):
            errors.append(f"{artifact_type}.speaker_ids 必须是非空且不重复的 speaker ID 列表")
    else:
        normalized_speaker_ids = []
    edited_turns = artifact.get("edited_turns")
    if isinstance(edited_turns, list):
        seen_turn_ids: set[str] = set()
        seen_sequences: set[int] = set()
        returned_speaker_ids: list[str] = []
        for index, turn in enumerate(edited_turns, start=1):
            path = f"{artifact_type}.edited_turns[{index}]"
            if not isinstance(turn, dict):
                errors.append(f"{path} 必须是 JSON object")
                continue
            required = {
                "turn_id",
                "sequence",
                "speaker_id",
                "source_sha256",
                "edited_text",
            }
            missing = sorted(required - set(turn))
            if missing:
                errors.append(f"{path} 缺少字段: {', '.join(missing)}")
                continue
            turn_id = str(turn.get("turn_id") or "")
            if not turn_id:
                errors.append(f"{path}.turn_id 不得为空")
            elif turn_id in seen_turn_ids:
                errors.append(f"{artifact_type}.edited_turns turn_id 重复: {turn_id}")
            seen_turn_ids.add(turn_id)
            sequence = turn.get("sequence")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
                errors.append(f"{path}.sequence 必须是正整数")
            elif sequence in seen_sequences:
                errors.append(f"{artifact_type}.edited_turns sequence 重复: {sequence}")
            else:
                seen_sequences.add(sequence)
            turn_speaker_id = str(turn.get("speaker_id") or "")
            if turn_speaker_id not in normalized_speaker_ids:
                errors.append(f"{path}.speaker_id 不在 artifact speaker_ids 中")
            elif turn_speaker_id not in returned_speaker_ids:
                returned_speaker_ids.append(turn_speaker_id)
            if not HEX_SHA256.fullmatch(str(turn.get("source_sha256") or "")):
                errors.append(f"{path}.source_sha256 必须是小写 SHA-256")
            if not str(turn.get("edited_text") or "").strip():
                errors.append(f"{path}.edited_text 不得为空")
        if returned_speaker_ids != normalized_speaker_ids:
            errors.append(f"{artifact_type}.speaker_ids 与 edited_turns 首次出现顺序不一致")
    return errors


def validate_editing_assembly_receipt(artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    errors: list[str] = []
    for field in ("manifest_sha256", "edit_artifact_digest", "assembled_draft_sha256"):
        if not HEX_SHA256.fullmatch(str(artifact.get(field) or "")):
            errors.append(f"editing_assembly_receipt.{field} 必须是小写 SHA-256")
    if artifact.get("status") != "assembled":
        errors.append("editing_assembly_receipt.status 必须是 assembled")
    turn_ids = artifact.get("ordered_turn_ids")
    if isinstance(turn_ids, list):
        normalized = [str(item) for item in turn_ids]
        if not normalized or any(not item for item in normalized):
            errors.append("editing_assembly_receipt.ordered_turn_ids 不得为空")
        elif len(normalized) != len(set(normalized)):
            errors.append("editing_assembly_receipt.ordered_turn_ids 不得重复")
    return errors


def validate_source_manifest(artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    errors: list[str] = []
    if artifact.get("source_mode") not in SOURCE_MODES:
        errors.append("source_manifest.source_mode 必须是固定枚举值")
    if artifact.get("archive_status") not in ARCHIVE_STATUSES:
        errors.append("source_manifest.archive_status 必须是固定枚举值")
    if artifact.get("archive_allowed") is False and artifact.get("archive_status") == "completed":
        errors.append("source_manifest archive_allowed=false 时不得报告 archive_status=completed")
    materials = artifact.get("materials")
    if isinstance(materials, list):
        if not materials:
            errors.append("source_manifest.materials 不得为空")
        for index, material in enumerate(materials, start=1):
            if not isinstance(material, dict):
                errors.append(f"source_manifest.materials 第 {index} 项必须是 JSON object")
                continue
            if not str(material.get("kind") or "").strip() or not str(material.get("name") or "").strip():
                errors.append(f"source_manifest.materials 第 {index} 项必须包含非空 kind 和 name")
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
    if not str(artifact.get("asr_primary") or "").strip():
        errors.append("transcript_audit.asr_primary 不得为空")
    if not str(artifact.get("timestamp_index_status") or "").strip():
        errors.append("transcript_audit.timestamp_index_status 不得为空")
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
        is_url = is_public_evidence_url(evidence)
        if not is_url and evidence not in PUBLIC_SOURCE_IDS:
            errors.append(
                "entity_verification_report external_evidence_paths 必须是公开 HTTPS URL 或公开 source ID（受支持枚举）: "
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


def validate_draft_review_binding(artifact_type: str, artifact: Any) -> list[str]:
    if not isinstance(artifact, dict):
        return []
    errors: list[str] = []
    if not str(artifact.get("reviewed_markdown_path") or "").strip():
        errors.append(f"{artifact_type}.reviewed_markdown_path 不得为空")
    digest = str(artifact.get("reviewed_markdown_sha256") or "").strip().lower()
    if not HEX_SHA256.fullmatch(digest):
        errors.append(f"{artifact_type}.reviewed_markdown_sha256 必须是 64 位小写 SHA-256")
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
    else:
        validator_names = {str(item.get("name") or "").strip() for item in validators}
        missing_validators = sorted(REQUIRED_VALIDATOR_NAMES - validator_names)
        unknown_validators = sorted(validator_names - REQUIRED_VALIDATOR_NAMES)
        if missing_validators:
            errors.append("export_manifest.validators_run 缺少必需 validator: " + ", ".join(missing_validators))
        if unknown_validators:
            errors.append("export_manifest.validators_run 包含未知 validator: " + ", ".join(unknown_validators))
    regression = artifact.get("regression_result")
    if not isinstance(regression, dict) or not isinstance(regression.get("ok"), bool):
        errors.append("export_manifest.regression_result 必须包含 boolean ok")
    elif (
        regression.get("name") != REGRESSION_VALIDATOR_NAME
        or not isinstance(regression.get("case_count"), int)
        or isinstance(regression.get("case_count"), bool)
        or int(regression.get("case_count")) <= 0
    ):
        errors.append(
            "export_manifest.regression_result 必须包含 name=run_meeting_minutes_regression.py 和正整数 case_count"
        )
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
        sidecar_value = item.get("是否需要 sidecar")
        if not isinstance(sidecar_value, bool):
            errors.append(f"doubtful_items 第 {index + 1} 条是否需要 sidecar 必须是 boolean")
        elif doubtful_type in NON_BUSINESS_DOUBTFUL_TYPES and sidecar_value:
            errors.append(f"doubtful_items 第 {index + 1} 条人名或说话人身份不得进入 sidecar")
        elif doubtful_type in ALLOWED_DOUBTFUL_TYPES - NON_BUSINESS_DOUBTFUL_TYPES and not sidecar_value:
            errors.append(f"doubtful_items 第 {index + 1} 条非人名业务存疑必须进入 sidecar")
        for field in DOUBTFUL_REQUIRED_FIELDS:
            if field == "是否需要 sidecar":
                continue
            if field in item and not isinstance(item[field], str):
                errors.append(f"doubtful_items 第 {index + 1} 条 {field} 必须是 string")
        for field in ("原始表述", "当前判断", "上下文依据", "检索/证据路径", "最终处理"):
            if isinstance(item.get(field), str) and not item[field].strip():
                errors.append(f"doubtful_items 第 {index + 1} 条 {field} 不得为空")
    return errors


def validate_cross_artifact_consistency(artifacts: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    doubtful_items = artifacts.get("doubtful_items")
    doubtful_raw = {
        str(item.get("原始表述") or "").strip()
        for item in doubtful_items
        if isinstance(doubtful_items, list) and isinstance(item, dict) and str(item.get("原始表述") or "").strip()
    } if isinstance(doubtful_items, list) else set()
    entity_report = artifacts.get("entity_verification_report")
    if isinstance(entity_report, dict) and "doubtful_items" in artifacts:
        unresolved = {
            str(item).strip()
            for item in as_list(entity_report.get("unresolved_items"))
            if str(item).strip()
        }
        missing = sorted(unresolved - doubtful_raw)
        if missing:
            errors.append("entity_verification_report.unresolved_items 缺少对应 doubtful_items: " + ", ".join(missing))
    export_manifest = artifacts.get("export_manifest")
    if isinstance(export_manifest, dict) and "doubtful_items" in artifacts:
        known_unverified = {
            str(item).strip()
            for item in as_list(export_manifest.get("known_unverified_parts"))
            if str(item).strip()
        }
        missing = sorted(known_unverified - doubtful_raw)
        if missing:
            errors.append("export_manifest.known_unverified_parts 缺少对应 doubtful_items: " + ", ".join(missing))
    return errors


def validate_payload(payload: Any, required_artifacts: list[str] | None = None) -> dict[str, Any]:
    artifacts, errors = artifact_mapping(payload)
    errors.extend(forbidden_field_errors(payload, "payload"))
    warnings: list[str] = []
    required_artifacts = required_artifacts or []

    for artifact_type in required_artifacts:
        if artifact_type not in artifacts:
            errors.append(f"缺少必需 artifact: {artifact_type}")

    for artifact_type, artifact in artifacts.items():
        path = f"artifacts.{artifact_type}"
        artifact_schema = artifact_schema_name(str(artifact_type))
        if artifact_type == "doubtful_items":
            errors.extend(validate_doubtful_items(artifact))
        elif artifact_schema in REQUIRED_FIELDS:
            errors.extend(validate_required_fields(artifact_schema, artifact))
            errors.extend(validate_field_types(artifact_schema, artifact))
            if artifact_schema == "source_manifest":
                errors.extend(validate_source_manifest(artifact))
            elif artifact_schema == "transcript_audit":
                errors.extend(validate_transcript_audit(artifact))
            elif artifact_schema == "source_reconciliation":
                errors.extend(validate_source_reconciliation(artifact))
            elif artifact_schema == "entity_verification_report":
                errors.extend(validate_entity_verification_report(artifact))
            elif artifact_schema in {"target_attribution_review", "fidelity_review"}:
                errors.extend(validate_draft_review_binding(artifact_schema, artifact))
            elif artifact_schema == "speaker_turn_edit":
                errors.extend(validate_speaker_turn_edit(str(artifact_type), artifact))
            elif artifact_schema == "editing_assembly_receipt":
                errors.extend(validate_editing_assembly_receipt(artifact))
            elif artifact_schema == "export_manifest":
                errors.extend(validate_export_manifest(artifact))
            elif artifact_schema == "main_action_receipt":
                errors.extend(validate_main_action_receipt(artifact))
        else:
            errors.append(f"未知 MAS artifact 类型: {artifact_type}")
    errors.extend(validate_cross_artifact_consistency(artifacts))

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
