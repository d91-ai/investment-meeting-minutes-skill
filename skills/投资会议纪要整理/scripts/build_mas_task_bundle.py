#!/usr/bin/env python3
"""Build deterministic MAS specialist task bundles for meeting minutes."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from create_mas_source_manifest import material_coverage_errors
from mas_task_lock import mas_task_lock
from validate_mas_artifacts import (
    BOOLEAN_FIELD_RULES,
    DOUBTFUL_REQUIRED_FIELDS,
    FORBIDDEN_FINAL_FIELDS,
    HEX_SHA256,
    LIST_FIELD_RULES,
    REQUIRED_FIELDS,
    SAFE_DYNAMIC_ARTIFACT,
    SPEAKER_TURN_EDIT_PREFIX,
    STRING_FIELD_RULES,
    artifact_schema_name,
    canonical_json_digest,
)

RUN_PROFILES = {"fast_document", "standard", "strict_audio"}
SOURCE_MODES = {"document_only", "audio_only", "audio_plus_document"}
MEETING_TYPES = {"多人复盘会", "公司交流", "专家交流"}
SOURCE_SELECTION_STATUSES = {"not_applicable", "not_compared", "compared_clear", "conflict", "uncertain"}
SPEAKER_EDITING_MODES = {"auto", "skip", "full"}
AUTO_PARALLEL_SOURCE_CHARS = 16_000
MAX_SPEAKER_EDIT_SHARD_CHARS = 16_000
SKILL_INSTRUCTION_PATH = Path(__file__).resolve().parent.parent / "SKILL.md"
PRIMARY_SOURCE_ALIASES_BY_MODE = {
    "audio_only": {"aligned_transcript", "audio_transcript", "transcript"},
    "document_only": {"document", "provided_document", "provided_transcript", "transcript"},
    "audio_plus_document": {
        "aligned_transcript",
        "audio_transcript",
        "document",
        "provided_document",
        "provided_transcript",
        "transcript",
    },
}
PRIMARY_SOURCE_EXAMPLE_BY_MODE = {
    "audio_only": "aligned_transcript",
    "document_only": "provided_document",
    "audio_plus_document": "aligned_transcript",
}

AUDIO_RISKS = {
    "audio_input",
    "long_audio",
    "noisy_audio",
    "unclear_speaker_boundaries",
    "timestamp_alignment",
    "strict_audio",
}
SOURCE_RECONCILIATION_RISKS = {
    "audio_plus_document",
    "source_conflict",
    "primary_source_uncertain",
}
ENTITY_RISKS = {
    "entity_verification",
    "high_risk_facts",
    "many_doubtful_items",
    "company_codes",
    "customers_suppliers",
    "numbers_dates",
}
TARGET_RISKS = {
    "target_attribution",
    "multi_target",
    "mixed_targets",
    "positive_negative_views",
}
FIDELITY_RISKS = {
    "fidelity_review",
    "omission_risk",
    "summary_compression",
    "third_person_rewrite",
    "prior_user_feedback",
}
EDITING_RISKS = {
    "speaker_turn_editing",
    "long_transcript",
    "filler_cleanup",
}
KNOWN_RISK_FLAGS = (
    AUDIO_RISKS
    | SOURCE_RECONCILIATION_RISKS
    | ENTITY_RISKS
    | TARGET_RISKS
    | FIDELITY_RISKS
    | EDITING_RISKS
)

ROLE_SPECS: dict[str, dict[str, Any]] = {
    "transcript_audit": {
        "role": "Transcript Auditor",
        "dispatch_phase": "pre_draft",
        "objective": "Audit ASR quality, speaker boundaries, timestamp anchors, and ASR conflicts.",
        "inputs": [
            "raw audio metadata",
            "SenseVoice transcript",
            "Paraformer auxiliary differences",
            "timestamp_index",
        ],
        "checks": [
            "ASR noise",
            "long segment anomalies",
            "speaker-boundary ambiguity",
            "SenseVoice/Paraformer conflict",
            "timestamp anchor reliability",
        ],
    },
    "source_reconciliation": {
        "role": "Source Reconciler",
        "dispatch_phase": "pre_draft",
        "objective": "Select and justify the primary body source from same-session materials.",
        "inputs": [
            "audio-derived aligned_transcript",
            "provided document or transcript",
            "same-session user corrections",
            "source quality notes",
        ],
        "checks": [
            "coverage",
            "speaker order",
            "verbatimness",
            "timestamp evidence",
            "ASR noise versus human-correction traces",
            "omissions",
            "source conflicts",
        ],
    },
    "entity_verification_report": {
        "role": "Entity Verifier",
        "dispatch_phase": "pre_draft",
        "objective": "Verify non-person business entities and update doubtful_items proposals.",
        "inputs": [
            "current-session source context",
            "entity candidates",
            "local code candidates",
            "external evidence paths",
        ],
        "checks": [
            "company names",
            "stock codes",
            "customers and suppliers",
            "numbers and dates",
            "industry terms",
            "public high-risk facts",
        ],
        "secondary_artifacts": ["doubtful_items"],
    },
    "target_attribution_review": {
        "role": "Target Attribution Reviewer",
        "dispatch_phase": "draft_review",
        "objective": "Review target headings, sector grouping, and positive/negative attribution.",
        "inputs": [
            "review-meeting draft body",
            "source spans",
            "entity verification status",
        ],
        "checks": [
            "wrong grouping",
            "missing positive targets",
            "missing codes for targets already included in target headings",
            "missing codes for securities targets mentioned in the body",
            "incidental targets in heading",
            "negative targets in target line",
            "non-source companies",
        ],
    },
    "fidelity_review": {
        "role": "Fidelity Reviewer",
        "dispatch_phase": "draft_review",
        "objective": "Review whether draft prose preserves source order, pronouns, and substance.",
        "inputs": [
            "draft Markdown",
            "source spans",
            "source_reconciliation",
        ],
        "checks": [
            "summary compression",
            "third-person rewrite",
            "omitted reasons or numbers",
            "merged speaker turns",
            "speaker-order drift",
        ],
    },
    "speaker_turn_edit": {
        "role": "Speaker Turn Editor",
        "dispatch_phase": "editing",
        "objective": "Apply the main workflow's exact base SKILL.md to one assigned source shard.",
        "inputs": [
            "one assigned shard from the current speaker_turn_manifest",
        ],
        "checks": [
            "exact base SKILL.md version",
            "exact assigned-turn coverage and order",
            "assigned shard only",
        ],
    },
    "export_manifest": {
        "role": "Contract Verifier",
        "dispatch_phase": "final_verification",
        "objective": "Verify encoding, Markdown contract, sidecar consistency, export status, and regressions.",
        "inputs": [
            "final Markdown",
            "verification sidecar",
            "timestamp_index",
            "export logs",
            "validator outputs",
        ],
        "checks": [
            "UTF-8",
            "Markdown contract",
            "doubtful table",
            "timestamp_index",
            "verification sidecar",
            "regression result",
        ],
    },
}

DISPATCH_PHASES: dict[str, dict[str, str]] = {
    "pre_draft": {
        "when": "After current-session source materials are prepared and before final-note drafting.",
        "materials": "Audio/transcript/document excerpts, timestamp indexes, entity candidates, and source-quality notes relevant to the assigned role.",
    },
    "editing": {
        "when": "After pre-draft source decisions and before the main workflow assembles the draft body.",
        "materials": "Only the assigned speaker-turn shard from the current speaker_turn_manifest plus narrowly relevant current-session context.",
    },
    "draft_review": {
        "when": "After the main workflow has a draft and before final validation.",
        "materials": "Draft Markdown excerpts plus source spans and validated process artifacts required by the assigned role.",
    },
    "final_verification": {
        "when": "After final Markdown, sidecars, export logs, and validator outputs exist.",
        "materials": "Final Markdown path, verification sidecar path, timestamp index, export logs, and validator/regression results.",
    },
}

PHASE_ORDER = {phase: index for index, phase in enumerate(DISPATCH_PHASES)}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def skill_instruction_sha256() -> str:
    return hashlib.sha256(SKILL_INSTRUCTION_PATH.read_bytes()).hexdigest()


def normalized_flags(flags: Any) -> list[str]:
    if flags is None:
        return []
    if not isinstance(flags, list):
        raise ValueError("risk_flags 必须是 JSON array")
    normalized = sorted({str(flag).strip() for flag in flags if str(flag).strip()})
    unknown = sorted(set(normalized) - KNOWN_RISK_FLAGS)
    if unknown:
        raise ValueError("未知 risk_flags: " + ", ".join(unknown))
    return normalized


def normalized_speaker_turn_manifest(value: Any) -> dict[str, Any] | None:
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ValueError("speaker_turn_manifest 必须是 JSON object")
    manifest = copy.deepcopy(value)
    if manifest.get("schema_version") != "1.0":
        raise ValueError("speaker_turn_manifest schema_version 必须是 1.0")
    turns = manifest.get("turns")
    shards = manifest.get("shards")
    if not isinstance(turns, list) or not turns:
        raise ValueError("speaker_turn_manifest.turns 必须是非空 JSON array")
    if not isinstance(shards, list) or not shards:
        raise ValueError("speaker_turn_manifest.shards 必须是非空 JSON array")
    turn_by_id: dict[str, dict[str, Any]] = {}
    sequences: set[int] = set()
    for index, turn in enumerate(turns, start=1):
        if not isinstance(turn, dict):
            raise ValueError(f"speaker_turn_manifest.turns[{index}] 必须是 JSON object")
        turn_id = str(turn.get("turn_id") or "")
        speaker_id = str(turn.get("speaker_id") or "")
        sequence = turn.get("sequence")
        if not turn_id or turn_id in turn_by_id:
            raise ValueError("speaker_turn_manifest turn_id 必须唯一且非空")
        if not speaker_id:
            raise ValueError(f"speaker_turn_manifest {turn_id} speaker_id 不得为空")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0 or sequence in sequences:
            raise ValueError("speaker_turn_manifest sequence 必须是唯一正整数")
        source_sha256 = str(turn.get("source_sha256") or "")
        text = str(turn.get("text") or "")
        if not HEX_SHA256.fullmatch(source_sha256):
            raise ValueError(f"speaker_turn_manifest {turn_id} source_sha256 必须是小写 SHA-256")
        if not text.strip():
            raise ValueError(f"speaker_turn_manifest {turn_id} text 不得为空")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != source_sha256:
            raise ValueError(f"speaker_turn_manifest {turn_id} source_sha256 与 text 不一致")
        turn_by_id[turn_id] = turn
        sequences.add(sequence)
    if sequences != set(range(1, len(turns) + 1)):
        raise ValueError("speaker_turn_manifest sequence 必须从 1 连续递增")

    assigned_turns: list[str] = []
    artifact_types: set[str] = set()
    shard_ids: set[str] = set()
    previous_shard_last_sequence = 0
    for index, shard in enumerate(shards, start=1):
        if not isinstance(shard, dict):
            raise ValueError(f"speaker_turn_manifest.shards[{index}] 必须是 JSON object")
        shard_id = str(shard.get("shard_id") or "")
        artifact_type = str(shard.get("artifact_type") or "")
        speaker_ids = shard.get("speaker_ids")
        speaker_labels = shard.get("speaker_labels")
        turn_ids = shard.get("turn_ids")
        if not shard_id or shard_id in shard_ids:
            raise ValueError("speaker_turn_manifest shard_id 必须唯一且非空")
        if (
            not artifact_type.startswith(SPEAKER_TURN_EDIT_PREFIX)
            or not SAFE_DYNAMIC_ARTIFACT.fullmatch(artifact_type)
            or artifact_type in artifact_types
        ):
            raise ValueError("speaker_turn_manifest artifact_type 必须是唯一安全的 speaker_turn_edit__ key")
        if not isinstance(turn_ids, list) or not turn_ids:
            raise ValueError(f"speaker_turn_manifest shard {shard_id} turn_ids 必须是非空 array")
        normalized_turn_ids = [str(item) for item in turn_ids]
        if len(normalized_turn_ids) != len(set(normalized_turn_ids)):
            raise ValueError(f"speaker_turn_manifest shard {shard_id} turn_ids 不得重复")
        unknown = sorted(set(normalized_turn_ids) - set(turn_by_id))
        if unknown:
            raise ValueError(f"speaker_turn_manifest shard {shard_id} 包含未知 turn_id: {', '.join(unknown)}")
        shard_sequences = [
            int(turn_by_id[turn_id].get("sequence") or 0)
            for turn_id in normalized_turn_ids
        ]
        if shard_sequences != sorted(shard_sequences):
            raise ValueError(f"speaker_turn_manifest shard {shard_id} turn_ids 必须保持全局 sequence 顺序")
        if shard_sequences != list(range(shard_sequences[0], shard_sequences[-1] + 1)):
            raise ValueError(f"speaker_turn_manifest shard {shard_id} 只能包含连续 sequence")
        if shard_sequences[0] <= previous_shard_last_sequence:
            raise ValueError("speaker_turn_manifest shards 必须按全局 sequence 排列")
        previous_shard_last_sequence = shard_sequences[-1]
        expected_speaker_ids = list(
            dict.fromkeys(
                str(turn_by_id[turn_id].get("speaker_id") or "")
                for turn_id in normalized_turn_ids
            )
        )
        expected_speaker_labels = list(
            dict.fromkeys(
                str(turn_by_id[turn_id].get("speaker_label") or "")
                for turn_id in normalized_turn_ids
            )
        )
        if speaker_ids != expected_speaker_ids:
            raise ValueError(
                f"speaker_turn_manifest shard {shard_id} speaker_ids 与 turns 不一致"
            )
        if speaker_labels != expected_speaker_labels:
            raise ValueError(
                f"speaker_turn_manifest shard {shard_id} speaker_labels 与 turns 不一致"
            )
        input_payload = {
            "source_name": str(manifest.get("source_name") or ""),
            "source_sha256": str(manifest.get("source_sha256") or ""),
            "speaker_ids": expected_speaker_ids,
            "speaker_labels": expected_speaker_labels,
            "turns": [turn_by_id[turn_id] for turn_id in normalized_turn_ids],
        }
        if str(shard.get("input_sha256") or "") != canonical_json_digest(input_payload):
            raise ValueError(f"speaker_turn_manifest shard {shard_id} input_sha256 与内容不一致")
        actual_char_count = sum(len(str(turn_by_id[turn_id].get("text") or "")) for turn_id in normalized_turn_ids)
        if shard.get("char_count") != actual_char_count:
            raise ValueError(f"speaker_turn_manifest shard {shard_id} char_count 与内容不一致")
        if actual_char_count > MAX_SPEAKER_EDIT_SHARD_CHARS:
            raise ValueError(
                f"speaker_turn_manifest shard {shard_id} 超过 {MAX_SPEAKER_EDIT_SHARD_CHARS} 字符硬上限"
            )
        assigned_turns.extend(normalized_turn_ids)
        artifact_types.add(artifact_type)
        shard_ids.add(shard_id)
    if len(assigned_turns) != len(set(assigned_turns)) or set(assigned_turns) != set(turn_by_id):
        raise ValueError("speaker_turn_manifest shards 必须恰好覆盖全部 turns 一次")
    if manifest.get("turn_count") != len(turns) or manifest.get("shard_count") != len(shards):
        raise ValueError("speaker_turn_manifest turn_count/shard_count 与内容不一致")
    declared_digest = str(manifest.get("manifest_sha256") or "")
    digest_payload = {key: item for key, item in manifest.items() if key != "manifest_sha256"}
    if declared_digest != canonical_json_digest(digest_payload):
        raise ValueError("speaker_turn_manifest manifest_sha256 与内容不一致")
    return manifest


def resolve_speaker_editing(
    requested_mode: Any,
    manifest: dict[str, Any] | None,
    materials: list[Any],
) -> dict[str, Any]:
    del materials
    mode = str(requested_mode or "auto").strip()
    validate_choice("speaker_editing_mode", mode, SPEAKER_EDITING_MODES)
    if manifest is None:
        if mode == "full":
            raise ValueError("speaker_editing_mode=full 必须提供 speaker_turn_manifest")
        return {
            "requested_mode": mode,
            "effective_mode": "skip" if mode == "skip" else "not_applicable",
            "reason": (
                "explicit_main_workflow_editing"
                if mode == "skip"
                else "speaker_turn_manifest_not_provided"
            ),
            "source_char_count": 0,
            "shard_count": 0,
            "max_shard_chars": 0,
            "parallel_threshold_chars": AUTO_PARALLEL_SOURCE_CHARS,
        }

    source_char_count = sum(
        len(str(turn.get("text") or ""))
        for turn in manifest.get("turns", [])
        if isinstance(turn, dict)
    )
    shard_char_counts = [
        int(shard.get("char_count") or 0)
        for shard in manifest.get("shards", [])
        if isinstance(shard, dict)
    ]
    if mode == "auto":
        if source_char_count > AUTO_PARALLEL_SOURCE_CHARS:
            effective_mode = "full"
            reason = "source_exceeds_parallel_context_threshold"
        else:
            effective_mode = "skip"
            reason = "source_fits_main_workflow_context"
    else:
        effective_mode = mode
        reason = (
            "explicit_parallel_editing"
            if mode == "full"
            else "explicit_main_workflow_editing"
        )
    return {
        "requested_mode": mode,
        "effective_mode": effective_mode,
        "reason": reason,
        "source_char_count": source_char_count,
        "shard_count": len(shard_char_counts),
        "max_shard_chars": max(shard_char_counts, default=0),
        "parallel_threshold_chars": AUTO_PARALLEL_SOURCE_CHARS,
    }


def validate_choice(name: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"{name} 必须是以下之一: {allowed_text}")


def bundle_configuration_errors(
    run_profile: str,
    source_mode: str,
    meeting_type: str,
    source_selection_status: str,
) -> list[str]:
    errors: list[str] = []
    if run_profile not in RUN_PROFILES:
        errors.append("run_profile 必须是固定枚举值")
    if source_mode not in SOURCE_MODES:
        errors.append("source_mode 必须是固定枚举值")
    if meeting_type not in MEETING_TYPES:
        errors.append("meeting_type 必须是固定枚举值")
    if source_selection_status not in SOURCE_SELECTION_STATUSES:
        errors.append("source_selection_status 必须是固定枚举值")
    if source_mode == "audio_only" and run_profile != "strict_audio":
        errors.append("audio_only 必须使用 strict_audio run_profile")
    if source_mode == "audio_plus_document" and source_selection_status == "not_applicable":
        errors.append("audio_plus_document 的 source_selection_status 不得为 not_applicable")
    if source_mode in {"audio_only", "document_only"} and source_selection_status != "not_applicable":
        errors.append("非混合来源的 source_selection_status 必须为 not_applicable")
    return errors


def normalized_source_selection_status(source_mode: str, value: Any) -> str:
    status = str(value or "").strip()
    if not status:
        return "not_compared" if source_mode == "audio_plus_document" else "not_applicable"
    validate_choice("source_selection_status", status, SOURCE_SELECTION_STATUSES)
    if source_mode == "audio_plus_document" and status == "not_applicable":
        return "not_compared"
    if source_mode != "audio_plus_document" and status != "not_applicable":
        raise ValueError("source_selection_status 仅适用于 audio_plus_document；其他 source_mode 请使用 not_applicable")
    return status


def infer_risk_flags(
    run_profile: str,
    source_mode: str,
    risk_flags: list[str],
    source_selection_status: str = "not_applicable",
) -> list[str]:
    risks = set(risk_flags)
    if source_mode == "audio_only":
        risks.update({"audio_input", "timestamp_alignment"})
    if source_mode == "audio_plus_document":
        if source_selection_status in {"not_compared", "uncertain"}:
            risks.add("primary_source_uncertain")
        elif source_selection_status == "conflict":
            risks.update({"primary_source_uncertain", "source_conflict"})
    if run_profile == "strict_audio":
        risks.update({"audio_input", "strict_audio", "long_audio", "timestamp_alignment", "omission_risk"})
    return sorted(risks)


def should_use_mas(
    run_profile: str,
    source_mode: str,
    risk_flags: list[str],
    source_selection_status: str = "not_applicable",
) -> bool:
    return True


def select_expected_artifacts(
    run_profile: str,
    source_mode: str,
    meeting_type: str,
    risk_flags: list[str],
    source_selection_status: str = "not_applicable",
) -> list[str]:
    risks = set(infer_risk_flags(run_profile, source_mode, risk_flags, source_selection_status))
    if not should_use_mas(run_profile, source_mode, risk_flags, source_selection_status):
        return []

    artifacts = {"source_manifest", "export_manifest"}
    if risks & AUDIO_RISKS or (
        source_mode == "audio_plus_document" and source_selection_status != "compared_clear"
    ):
        artifacts.add("transcript_audit")
    if risks & SOURCE_RECONCILIATION_RISKS:
        artifacts.add("source_reconciliation")
    if risks & TARGET_RISKS or (meeting_type == "多人复盘会" and run_profile == "strict_audio"):
        artifacts.add("target_attribution_review")
    if risks & ENTITY_RISKS:
        artifacts.add("entity_verification_report")
        artifacts.add("doubtful_items")
    if risks & (FIDELITY_RISKS | SOURCE_RECONCILIATION_RISKS):
        artifacts.add("fidelity_review")
    return sorted(artifacts)


def output_shape_for(
    artifact_type: str,
    secondary_artifacts: list[str],
    identity: dict[str, str] | None = None,
    source_mode: str = "audio_plus_document",
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_schema = artifact_schema_name(artifact_type)

    def placeholder(field: str) -> Any:
        artifact_examples: dict[str, dict[str, Any]] = {
            "transcript_audit": {
                "asr_primary": "SenseVoiceSmall",
                "asr_auxiliary": "",
                "timestamp_index_status": "unavailable",
                "recommended_action": "continue",
            },
            "source_reconciliation": {
                "primary_body_source": PRIMARY_SOURCE_EXAMPLE_BY_MODE.get(source_mode, "transcript"),
                "primary_source_reason": "replace with current-session evidence",
                "cross_check_source": "provided_document" if source_mode == "audio_plus_document" else "",
                "manual_review_required": False,
            },
            "target_attribution_review": {"segments_reviewed": 1},
            "fidelity_review": {"paragraphs_reviewed": 1},
            "speaker_turn_edit": {
                "manifest_sha256": str((task_context or {}).get("manifest_sha256") or "0" * 64),
                "shard_id": str((task_context or {}).get("shard_id") or "package_001"),
                "speaker_ids": [
                    str(item)
                    for item in (task_context or {}).get("speaker_ids", [])
                ],
                "input_sha256": str((task_context or {}).get("input_sha256") or "0" * 64),
                "status": "complete",
                "edited_turns": [
                    {
                        "turn_id": str(turn.get("turn_id") or ""),
                        "sequence": turn.get("sequence", 1),
                        "speaker_id": str(turn.get("speaker_id") or ""),
                        "source_sha256": str(turn.get("source_sha256") or ""),
                        "edited_text": "<edited source text>",
                    }
                    for turn in (task_context or {}).get("turns", [])
                    if isinstance(turn, dict)
                ],
                "unresolved_spans": [],
            },
            "export_manifest": {
                "markdown_path": "NOTE.md",
                "markdown_sha256": "0" * 64,
                "verification_sidecar_path": "",
                "validators_run": [
                    {"name": "validate_utf8_text.py", "ok": False},
                    {"name": "validate_meeting_minutes_contract.py", "ok": False},
                ],
                "regression_result": {
                    "name": "run_meeting_minutes_regression.py",
                    "case_count": 1,
                    "ok": False,
                },
                "export_status": "blocked",
                "main_actions_verified": False,
            },
        }
        if field in artifact_examples.get(artifact_schema, {}):
            return copy.deepcopy(artifact_examples[artifact_schema][field])
        if field in BOOLEAN_FIELD_RULES.get(artifact_schema, []):
            return False
        if field in LIST_FIELD_RULES.get(artifact_schema, []):
            return []
        if field in STRING_FIELD_RULES.get(artifact_schema, []):
            return ""
        if field in {"segments_reviewed", "paragraphs_reviewed"}:
            return 1
        if field in {"confirmed_item_evidence_paths", "regression_result"}:
            return {}
        return None

    identity = identity or {}
    if secondary_artifacts:
        shape: dict[str, Any] = {
            **identity,
            "artifacts": {
                artifact_type: {field: placeholder(field) for field in REQUIRED_FIELDS[artifact_schema]},
            }
        }
        for secondary in secondary_artifacts:
            if secondary == "doubtful_items":
                shape["artifacts"][secondary] = []
        return shape
    return {
        **identity,
        "artifact_type": artifact_type,
        "artifact": {field: placeholder(field) for field in REQUIRED_FIELDS[artifact_schema]},
    }


def prompt_for_task(
    artifact_type: str,
    spec: dict[str, Any],
    run_profile: str,
    source_mode: str,
    task_context: dict[str, Any] | None = None,
) -> str:
    artifact_schema = artifact_schema_name(artifact_type)
    required_fields = REQUIRED_FIELDS[artifact_schema]
    secondary = [str(item) for item in spec.get("secondary_artifacts", [])]
    if artifact_schema == "speaker_turn_edit":
        context = task_context or {}
        lines = [
            "Use $investment-meeting-minutes for this Speaker Turn Editor task.",
            "Load and follow the exact same investment-meeting-minutes SKILL.md used by the Main Orchestrator.",
            "Apply that Skill's existing text-editing principles to every assigned turn; do not add another editing standard.",
            "Do not write, modify, assemble, or export final Markdown.",
            "Do not modify repository files or meeting-note files.",
            "Return only JSON: an array with one object per displayed turn, in the same order.",
            'Each object must contain only "turn_id" and "edited_text".',
            'Example: [{"turn_id":"turn_000001","edited_text":"整理后的该段文本"}]',
            "",
            "Assigned turns:",
        ]
        for turn in context.get("turns", []):
            if not isinstance(turn, dict):
                continue
            turn_id = str(turn.get("turn_id") or "")
            lines.extend(
                [
                    "",
                    (
                        f"--- {turn_id} | speaker={turn.get('speaker_label')} ---"
                    ),
                    str(turn.get("text") or ""),
                    f"--- end {turn_id} ---",
                ]
            )
        return "\n".join(lines)

    lines = [
        "Use $investment-meeting-minutes for this process-only specialist task.",
        f"Role: {spec['role']}.",
        f"Run profile: {run_profile}; source mode: {source_mode}.",
        f"Objective: {spec['objective']}",
        "Do not write, modify, assemble, or export final Markdown.",
        "Do not modify repository files or meeting-note files; return the requested process artifact only.",
        "Use only current-session meeting materials as meeting-content evidence.",
        "External sources may verify names, codes, terms, and public facts only.",
        "Do not upload private meeting materials, transcripts, recordings, or local paths to external services.",
        "Return only JSON. Do not include prose outside JSON.",
        f"Primary artifact: {artifact_type}.",
        "Role inputs: " + "; ".join(str(item) for item in spec.get("inputs", [])) + ".",
        "Required checks: " + "; ".join(str(item) for item in spec.get("checks", [])) + ".",
        f"Required fields: {', '.join(required_fields)}.",
    ]
    if "doubtful_items" in secondary:
        lines.append(f"Also return doubtful_items with fields: {', '.join(DOUBTFUL_REQUIRED_FIELDS)}.")
    if artifact_type == "transcript_audit":
        lines.append("Set recommended_action to exactly one of: continue, repair_transcript, request_user.")
        lines.append("Do not use continue when quality_flags, speaker_boundary_findings, or conflicts are non-empty.")
    elif artifact_type == "source_reconciliation":
        lines.append("When manual_review_required=false, primary_body_source and primary_source_reason must be non-empty.")
        aliases = ", ".join(sorted(PRIMARY_SOURCE_ALIASES_BY_MODE.get(source_mode, {"transcript"})))
        lines.append(
            "primary_body_source must name a current-session material or use an alias allowed for this source_mode: "
            + aliases
            + "."
        )
        if source_mode == "audio_plus_document":
            lines.append(
                "When manual_review_required=false, cross_check_source must be non-empty, bound to current-session "
                "audio/document evidence, and come from the evidence side not used by primary_body_source."
            )
        else:
            lines.append("If cross_check_source is non-empty, it must be bound to an eligible current-session body source.")
    elif artifact_type == "entity_verification_report":
        lines.append("Every items entry must appear in exactly one of confirmed_items or unresolved_items.")
        lines.append("Do not copy local_candidate_paths into external_evidence_paths.")
        lines.append("If entity evidence is insufficient, put the exact item in unresolved_items and doubtful_items; do not guess.")
    elif artifact_type == "target_attribution_review":
        lines.append("segments_reviewed must be a positive integer for the actual reviewed scope.")
        lines.append(
            "Decide whether a security belongs in a target heading from current-session context; do not promote every mention. "
            "For every security already included in a target heading, require its verified non-empty code. "
            "For every entity used as a securities target in the body, require its verified non-empty code even when it does "
            "not belong in the target heading; do not apply this to entities mentioned only as customers, suppliers, "
            "competitors, comparables, upstream/downstream entities, or background facts. Put a missing positive heading target "
            "or heading code in missing_positive_targets, and put every body target missing a code in recommended_revisions."
        )
        lines.append("If target attribution is unsupported, add the exact finding to recommended_revisions; do not invent a target.")
    elif artifact_schema == "fidelity_review":
        lines.append("paragraphs_reviewed must be a positive integer for the actual reviewed scope.")
        lines.append("If source mapping is insufficient, add the exact paragraph to source_mapping_failures; do not infer missing speech.")
    elif artifact_schema == "export_manifest":
        lines.append("Return markdown_sha256 for markdown_path and set main_actions_verified as a boolean.")
        lines.append("validators_run must contain exactly validate_utf8_text.py and validate_meeting_minutes_contract.py with boolean ok.")
        lines.append("regression_result must contain name=run_meeting_minutes_regression.py, a positive integer case_count, and boolean ok.")
        lines.append("Set export_status to exactly one of: passed, failed, blocked.")
    lines.append(f"Forbidden final-output fields: {', '.join(sorted(FORBIDDEN_FINAL_FIELDS))}.")
    if artifact_schema not in {"entity_verification_report", "target_attribution_review", "fidelity_review", "speaker_turn_edit"}:
        lines.append("If evidence is insufficient or conflicting, use this artifact's conflict or failure fields instead of guessing.")
    return "\n".join(lines)


def task_file_name(index: int, task: dict[str, Any]) -> str:
    artifact_type = str(task["artifact_type"])
    return f"{index:02d}-{artifact_type}.prompt.md"


def prompt_markdown(bundle: dict[str, Any], task: dict[str, Any]) -> str:
    artifact_type = str(task["artifact_type"])
    dispatch_phase = str(task["dispatch_phase"])
    phase = DISPATCH_PHASES[dispatch_phase]
    output_shape = json.dumps(task["expected_output_shape"], ensure_ascii=False, indent=2)
    if task.get("artifact_schema") == "speaker_turn_edit":
        return "\n".join(
            [
                "# MAS Speaker Turn Editor Task",
                "",
                "## Instructions and Assigned Source",
                "",
                str(task["prompt"]),
                "",
            ]
        )
    return "\n".join(
        [
            f"# MAS Specialist Task: {task['role']}",
            "",
            "Use this as the exact prompt for one Codex subagent when subagents are available.",
            "The main workflow remains the only final-note writer and final decision owner.",
            "",
            "## Run Context",
            "",
            f"- run_profile: `{bundle['run_profile']}`",
            f"- source_mode: `{bundle['source_mode']}`",
            f"- meeting_type: `{bundle['meeting_type']}`",
            f"- artifact_type: `{artifact_type}`",
            f"- artifact_schema: `{task.get('artifact_schema', artifact_schema_name(artifact_type))}`",
            f"- dispatch_phase: `{dispatch_phase}`",
            f"- run_id: `{bundle.get('run_id', '')}`",
            f"- task_id: `{task.get('task_id', '')}`",
            f"- artifact_owner: `{task.get('role', '')}`",
            f"- phase_timing: {phase['when']}",
            "",
            "## Material Handoff",
            "",
            "The main workflow must attach only the role-relevant current-session materials for this subagent.",
            f"Expected material class: {phase['materials']}",
            "Do not use this prompt alone as meeting-content evidence.",
            "Do not request or inspect unrelated repository files.",
            "",
            "## Prompt",
            "",
            "```text",
            str(task["prompt"]),
            "```",
            "",
            "## Expected JSON Shape",
            "",
            "```json",
            output_shape,
            "```",
            "",
        ]
    )


def build_task(
    artifact_type: str,
    run_profile: str,
    source_mode: str,
    task_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact_schema = artifact_schema_name(artifact_type)
    spec = ROLE_SPECS[artifact_schema]
    inputs = [str(item) for item in spec.get("inputs", [])]
    if artifact_schema == "fidelity_review" and source_mode != "audio_plus_document":
        inputs = [
            "selected primary body source and source-selection rationale"
            if item == "source_reconciliation"
            else item
            for item in inputs
        ]
    prompt_spec = {**spec, "inputs": inputs}
    secondary_artifacts = [str(item) for item in spec.get("secondary_artifacts", [])]
    dispatch_phase = str(spec["dispatch_phase"])
    skill_digest = skill_instruction_sha256() if artifact_schema == "speaker_turn_edit" else None
    task = {
        "role": spec["role"],
        "artifact_type": artifact_type,
        "artifact_schema": artifact_schema,
        "dispatch_phase": dispatch_phase,
        "secondary_artifacts": secondary_artifacts,
        "objective": spec["objective"],
        "inputs": inputs,
        "checks": spec["checks"],
        "required_fields": REQUIRED_FIELDS[artifact_schema],
        "forbidden_final_fields": sorted(FORBIDDEN_FINAL_FIELDS),
        "expected_output_shape": output_shape_for(
            artifact_type,
            secondary_artifacts,
            source_mode=source_mode,
            task_context=task_context,
        ),
        "prompt": prompt_for_task(
            artifact_type,
            prompt_spec,
            run_profile,
            source_mode,
            task_context=task_context,
        ),
        "material_handoff": DISPATCH_PHASES[dispatch_phase]["materials"],
    }
    if skill_digest is not None:
        task["skill_instruction_sha256"] = skill_digest
    if task_context is not None:
        task["task_context"] = copy.deepcopy(task_context)
    if "doubtful_items" in secondary_artifacts:
        task["secondary_required_fields"] = {"doubtful_items": DOUBTFUL_REQUIRED_FIELDS}
    return task


def bind_dispatch_identity(bundle: dict[str, Any]) -> dict[str, Any]:
    bound = copy.deepcopy(bundle)
    run_id = str(bound.get("run_id") or uuid.uuid4().hex)
    bound["run_id"] = run_id
    for index, task in enumerate(bound.get("tasks", []), start=1):
        if not isinstance(task, dict):
            continue
        artifact_type = str(task.get("artifact_type") or "")
        task_id = str(task.get("task_id") or f"{run_id}:{index:02d}:{artifact_type}")
        dispatch_phase = str(task.get("dispatch_phase") or "")
        owner = str(task.get("role") or "")
        task.update(
            {
                "run_id": run_id,
                "task_id": task_id,
                "artifact_owner": owner,
                "expected_output_shape": output_shape_for(
                    artifact_type,
                    [str(item) for item in task.get("secondary_artifacts", [])],
                    identity={
                        "run_id": run_id,
                        "task_id": task_id,
                        "dispatch_phase": dispatch_phase,
                        "artifact_owner": owner,
                    },
                    source_mode=str(bound.get("source_mode") or "audio_plus_document"),
                    task_context=task.get("task_context") if isinstance(task.get("task_context"), dict) else None,
                ),
            }
        )
    return bound


def dispatch_protocol() -> dict[str, Any]:
    return {
        "runtime": "codex_subagent_optional",
        "dispatch": "Spawn one read-only/process-only subagent per generated task file only when that task's dispatch_phase is ready; otherwise use the prompts as a manual checklist.",
        "parallelism": "Tasks in the same dispatch_phase may run in parallel after the main workflow has prepared role-relevant current-session materials.",
        "phases": DISPATCH_PHASES,
        "return_contract": "Speaker editors return only ordered turn_id/edited_text arrays; other subagents return the requested JSON artifact. The main workflow binds, validates, and consumes them.",
        "main_workflow_after_return": [
            "run_mas_phase_operator.py",
            "create_mas_source_manifest.py",
            "ingest_mas_artifact.py",
            "collect_mas_artifacts.py",
            "plan_mas_next_action.py",
            "validate_mas_artifacts.py",
            "summarize_mas_decisions.py",
            "revise or mark doubtful only through the main workflow",
            "run final Markdown validators",
        ],
    }


def artifact_owners(expected_artifacts: list[str]) -> dict[str, str]:
    owners: dict[str, str] = {}
    for artifact in expected_artifacts:
        if artifact in {"source_manifest", "editing_assembly_receipt"}:
            owners[artifact] = "Main Orchestrator"
        elif artifact == "doubtful_items":
            owners[artifact] = "Entity Verifier proposes; Main Orchestrator decides"
        elif artifact_schema_name(artifact) in ROLE_SPECS:
            owners[artifact] = str(ROLE_SPECS[artifact_schema_name(artifact)]["role"])
        else:
            owners[artifact] = "Main Orchestrator"
    return owners


def build_bundle_from_request(request: dict[str, Any]) -> dict[str, Any]:
    run_profile = str(request.get("run_profile") or "standard")
    source_mode = str(request.get("source_mode") or "document_only")
    meeting_type = str(request.get("meeting_type") or "多人复盘会")
    validate_choice("run_profile", run_profile, RUN_PROFILES)
    validate_choice("source_mode", source_mode, SOURCE_MODES)
    validate_choice("meeting_type", meeting_type, MEETING_TYPES)

    risk_flags = normalized_flags(request.get("risk_flags", request.get("risks", [])))
    materials = request.get("materials", [])
    if not isinstance(materials, list):
        raise ValueError("materials 必须是 JSON array")
    source_selection_status = normalized_source_selection_status(source_mode, request.get("source_selection_status"))
    configuration_errors = bundle_configuration_errors(
        run_profile,
        source_mode,
        meeting_type,
        source_selection_status,
    )
    if configuration_errors:
        raise ValueError("; ".join(configuration_errors))
    speaker_turn_manifest = normalized_speaker_turn_manifest(request.get("speaker_turn_manifest"))
    if (
        speaker_turn_manifest is not None
        and source_mode == "audio_plus_document"
        and source_selection_status != "compared_clear"
    ):
        raise ValueError("audio_plus_document 必须先确认正文主源，再生成 speaker_turn_manifest")
    speaker_editing = resolve_speaker_editing(
        request.get("speaker_editing_mode"),
        speaker_turn_manifest,
        materials,
    )
    speaker_editing_enabled = speaker_editing["effective_mode"] == "full"
    inferred_risks = infer_risk_flags(run_profile, source_mode, risk_flags, source_selection_status)
    if speaker_editing_enabled:
        inferred_risks = sorted(set(inferred_risks) | {"speaker_turn_editing"})
    expected_artifacts = select_expected_artifacts(
        run_profile,
        source_mode,
        meeting_type,
        risk_flags,
        source_selection_status,
    )
    if speaker_editing_enabled:
        expected_artifacts = sorted(
            set(expected_artifacts)
            | {"editing_assembly_receipt"}
            | {
                str(shard.get("artifact_type") or "")
                for shard in speaker_turn_manifest.get("shards", [])
                if isinstance(shard, dict)
            }
        )
    task_artifacts = [
        artifact
        for artifact in expected_artifacts
        if artifact not in {"source_manifest", "editing_assembly_receipt", "doubtful_items"}
    ]
    turn_by_id = {
        str(turn.get("turn_id") or ""): turn
        for turn in (speaker_turn_manifest or {}).get("turns", [])
        if isinstance(turn, dict)
    }
    shard_by_artifact = {
        str(shard.get("artifact_type") or ""): shard
        for shard in (speaker_turn_manifest or {}).get("shards", [])
        if isinstance(shard, dict)
    }
    tasks = []
    for artifact in task_artifacts:
        task_context = None
        if artifact_schema_name(artifact) == "speaker_turn_edit":
            shard = shard_by_artifact[artifact]
            task_context = {
                "manifest_sha256": str(speaker_turn_manifest.get("manifest_sha256") or ""),
                "shard_id": str(shard.get("shard_id") or ""),
                "speaker_ids": [
                    str(item) for item in shard.get("speaker_ids", [])
                ],
                "speaker_labels": [
                    str(item) for item in shard.get("speaker_labels", [])
                ],
                "input_sha256": str(shard.get("input_sha256") or ""),
                "turns": [
                    copy.deepcopy(turn_by_id[str(turn_id)])
                    for turn_id in shard.get("turn_ids", [])
                ],
            }
        tasks.append(build_task(artifact, run_profile, source_mode, task_context=task_context))
    tasks.sort(key=lambda task: (PHASE_ORDER[str(task["dispatch_phase"])], str(task["artifact_type"])))

    return {
        "schema_version": "1.0",
        "run_profile": run_profile,
        "source_mode": source_mode,
        "source_selection_status": source_selection_status,
        "meeting_type": meeting_type,
        "mas_required": should_use_mas(run_profile, source_mode, risk_flags, source_selection_status),
        "risk_flags": inferred_risks,
        "materials": copy.deepcopy(materials),
        "speaker_turn_manifest": speaker_turn_manifest,
        "speaker_editing": speaker_editing,
        "main_orchestrator": {
            "final_writer_only": True,
            "must_not_delegate": [
                "final Markdown writing",
                "archive/export side effects",
                "final user-facing delivery wording",
                "conflict decisions that require user confirmation",
            ],
            "decision_outputs": ["automatic_pass", "automatic_doubtful", "repair_required", "request_user"],
        },
        "expected_artifacts": expected_artifacts,
        "artifact_owners": artifact_owners(expected_artifacts),
        "dispatch_protocol": dispatch_protocol(),
        "tasks": tasks,
        "validation": {
            "artifact_validator": "scripts/validate_mas_artifacts.py",
            "required_artifacts": expected_artifacts,
        },
    }


def validate_bundle(
    bundle: dict[str, Any],
    *,
    require_material_coverage: bool = True,
) -> list[str]:
    errors: list[str] = []
    expected_artifacts = bundle.get("expected_artifacts")
    tasks = bundle.get("tasks")
    if bundle.get("schema_version") != "1.0":
        errors.append("MAS task bundle schema_version 必须是 1.0")
    run_profile = str(bundle.get("run_profile") or "")
    source_mode = str(bundle.get("source_mode") or "")
    meeting_type = str(bundle.get("meeting_type") or "")
    source_selection_status = str(bundle.get("source_selection_status") or "")
    configuration_errors = bundle_configuration_errors(
        run_profile,
        source_mode,
        meeting_type,
        source_selection_status,
    )
    errors.extend(configuration_errors)
    if not isinstance(expected_artifacts, list):
        errors.append("MAS task bundle expected_artifacts 必须是 JSON array")
        expected_artifacts = []
    if not isinstance(tasks, list):
        errors.append("MAS task bundle tasks 必须是 JSON array")
        tasks = []
    if not isinstance(bundle.get("materials"), list):
        errors.append("MAS task bundle materials 必须是 JSON array")
    elif require_material_coverage and bundle.get("mas_required") and not bundle.get("materials"):
        errors.append("MAS task bundle 启用 MAS 时 materials 不得为空")
    elif require_material_coverage:
        errors.extend(
            material_coverage_errors(
                str(bundle.get("source_mode") or ""),
                list(bundle.get("materials") or []),
            )
        )
    dispatch = bundle.get("dispatch_protocol")
    if bundle.get("mas_required") and not isinstance(dispatch, dict):
        errors.append("MAS task bundle dispatch_protocol 必须是 JSON object")
    owners = bundle.get("artifact_owners")
    if expected_artifacts and not isinstance(owners, dict):
        errors.append("MAS task bundle artifact_owners 必须是 JSON object")
        owners = {}
    if bundle.get("mas_required") and not tasks:
        errors.append("MAS task bundle 启用 MAS 时必须包含 specialist tasks")
    for artifact in expected_artifacts:
        if str(artifact) not in owners:
            errors.append(f"MAS task bundle artifact_owners 缺少 owner: {artifact}")

    expected_list = [str(item) for item in expected_artifacts]
    expected_set = set(expected_list)
    if len(expected_list) != len(expected_set):
        errors.append("MAS task bundle expected_artifacts 不得重复")
    risk_flags: list[str] = []
    speaker_turn_manifest: dict[str, Any] | None = None
    try:
        speaker_turn_manifest = normalized_speaker_turn_manifest(bundle.get("speaker_turn_manifest"))
    except ValueError as exc:
        errors.append(str(exc))
    if (
        speaker_turn_manifest is not None
        and source_mode == "audio_plus_document"
        and source_selection_status != "compared_clear"
    ):
        errors.append("audio_plus_document 必须先确认正文主源，再生成 speaker_turn_manifest")
    try:
        risk_flags = normalized_flags(bundle.get("risk_flags"))
    except ValueError as exc:
        errors.append(str(exc))
    else:
        if risk_flags != bundle.get("risk_flags"):
            errors.append("MAS task bundle risk_flags 必须去重并排序")
    speaker_editing: dict[str, Any] = {}
    try:
        declared_speaker_editing = bundle.get("speaker_editing")
        if not isinstance(declared_speaker_editing, dict):
            raise ValueError("MAS task bundle speaker_editing 必须是 JSON object")
        speaker_editing = resolve_speaker_editing(
            declared_speaker_editing.get("requested_mode"),
            speaker_turn_manifest,
            list(bundle.get("materials") or []),
        )
        if declared_speaker_editing != speaker_editing:
            errors.append("MAS task bundle speaker_editing 与 manifest/materials 自适应判定不一致")
    except ValueError as exc:
        errors.append(str(exc))
    speaker_editing_enabled = speaker_editing.get("effective_mode") == "full"
    if not isinstance(bundle.get("mas_required"), bool):
        errors.append("MAS task bundle mas_required 必须是 boolean")
    if not configuration_errors and isinstance(bundle.get("mas_required"), bool):
        canonical_mas_required = should_use_mas(
            run_profile,
            source_mode,
            risk_flags,
            source_selection_status,
        )
        if bundle.get("mas_required") != canonical_mas_required:
            errors.append("MAS task bundle mas_required 与 risk matrix 不一致")
        canonical_expected = set(
            select_expected_artifacts(
                run_profile,
                source_mode,
                meeting_type,
                risk_flags,
                source_selection_status,
            )
        )
        if speaker_editing_enabled:
            canonical_expected.add("editing_assembly_receipt")
            canonical_expected.update(
                str(shard.get("artifact_type") or "")
                for shard in speaker_turn_manifest.get("shards", [])
                if isinstance(shard, dict)
            )
        if expected_set != canonical_expected:
            errors.append(
                "MAS task bundle expected_artifacts 与 risk matrix 不一致: "
                f"expected={sorted(canonical_expected)} actual={sorted(expected_set)}"
            )
    if (
        bundle.get("source_mode") == "audio_plus_document"
        and "fidelity_review" in expected_set
        and "source_reconciliation" not in expected_set
    ):
        errors.append("audio_plus_document fidelity_review 缺少 source_reconciliation 依赖")
    produced_artifacts = {"source_manifest"} if bundle.get("mas_required") else set()
    producer_counts: dict[str, int] = {"source_manifest": 1} if bundle.get("mas_required") else {}
    if speaker_editing_enabled:
        produced_artifacts.add("editing_assembly_receipt")
        producer_counts["editing_assembly_receipt"] = 1
    manifest_turn_by_id = {
        str(turn.get("turn_id") or ""): turn
        for turn in (speaker_turn_manifest or {}).get("turns", [])
        if isinstance(turn, dict)
    }
    canonical_edit_contexts = {
        str(shard.get("artifact_type") or ""): {
            "manifest_sha256": str((speaker_turn_manifest or {}).get("manifest_sha256") or ""),
            "shard_id": str(shard.get("shard_id") or ""),
            "speaker_ids": [
                str(item) for item in shard.get("speaker_ids", [])
            ],
            "speaker_labels": [
                str(item) for item in shard.get("speaker_labels", [])
            ],
            "input_sha256": str(shard.get("input_sha256") or ""),
            "turns": [
                copy.deepcopy(manifest_turn_by_id[str(turn_id)])
                for turn_id in shard.get("turn_ids", [])
                if str(turn_id) in manifest_turn_by_id
            ],
        }
        for shard in (speaker_turn_manifest or {}).get("shards", [])
        if isinstance(shard, dict)
    }
    for task in tasks:
        if not isinstance(task, dict):
            errors.append("MAS task bundle task 必须是 JSON object")
            continue
        artifact_type = str(task.get("artifact_type") or "")
        artifact_schema = artifact_schema_name(artifact_type)
        if artifact_schema not in ROLE_SPECS:
            errors.append(f"未知 MAS task artifact_type: {artifact_type}")
            continue
        canonical_task_context = canonical_edit_contexts.get(artifact_type)
        if artifact_schema == "speaker_turn_edit" and task.get("task_context") != canonical_task_context:
            errors.append(f"{artifact_type} task_context 与 speaker_turn_manifest 不一致")
        expected_task = build_task(
            artifact_type,
            run_profile,
            source_mode,
            task_context=canonical_task_context,
        )
        for field in (
            "role",
            "artifact_schema",
            "dispatch_phase",
            "secondary_artifacts",
            "objective",
            "inputs",
            "checks",
            "required_fields",
            "forbidden_final_fields",
            "prompt",
            "material_handoff",
            "skill_instruction_sha256",
            "task_context",
        ):
            if task.get(field) != expected_task.get(field):
                errors.append(f"{artifact_type} task {field} 与角色契约不一致")
        if task.get("secondary_required_fields") != expected_task.get("secondary_required_fields"):
            errors.append(f"{artifact_type} task secondary_required_fields 与角色契约不一致")
        produced_artifacts.add(artifact_type)
        producer_counts[artifact_type] = producer_counts.get(artifact_type, 0) + 1
        actual_secondary = task.get("secondary_artifacts")
        if isinstance(actual_secondary, list):
            for item in actual_secondary:
                secondary = str(item)
                produced_artifacts.add(secondary)
                producer_counts[secondary] = producer_counts.get(secondary, 0) + 1
        if artifact_type not in expected_set:
            errors.append(f"task artifact_type 不在 expected_artifacts 中: {artifact_type}")
        required_fields = task.get("required_fields")
        if artifact_schema in REQUIRED_FIELDS and required_fields != REQUIRED_FIELDS[artifact_schema]:
            errors.append(f"{artifact_type} task required_fields 与 artifact schema 不一致")
        dispatch_phase = str(task.get("dispatch_phase") or "")
        if dispatch_phase not in DISPATCH_PHASES:
            errors.append(f"{artifact_type} task dispatch_phase 不合法: {dispatch_phase}")
        if not task.get("material_handoff"):
            errors.append(f"{artifact_type} task 缺少 material_handoff")
        prompt = str(task.get("prompt") or "")
        if "Do not write, modify, assemble, or export final Markdown." not in prompt:
            errors.append(f"{artifact_type} task prompt 缺少终稿写作边界")
        if "Return only JSON" not in prompt:
            errors.append(f"{artifact_type} task prompt 缺少 JSON-only 输出要求")
        if "Do not modify repository files or meeting-note files" not in prompt:
            errors.append(f"{artifact_type} task prompt 缺少文件写入边界")
    if produced_artifacts != expected_set:
        missing_producers = sorted(expected_set - produced_artifacts)
        unexpected_producers = sorted(produced_artifacts - expected_set)
        if missing_producers:
            errors.append("MAS task bundle 缺少 artifact 生产者: " + ", ".join(missing_producers))
        if unexpected_producers:
            errors.append("MAS task bundle 包含未声明 artifact 生产者: " + ", ".join(unexpected_producers))
    duplicate_producers = sorted(
        artifact_type
        for artifact_type, count in producer_counts.items()
        if count != 1
    )
    if duplicate_producers:
        errors.append("MAS task bundle artifact 必须恰好一个生产者: " + ", ".join(duplicate_producers))
    return errors


def _write_dispatch_files_unlocked(
    bundle: dict[str, Any],
    task_dir: Path,
    *,
    overwrite_prompts: bool = False,
) -> dict[str, Any]:
    task_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir = task_dir / "artifacts"
    existing_dispatch_files = [
        path
        for path in [
            task_dir / "mas_task_bundle.json",
            task_dir / "dispatch_manifest.json",
            *task_dir.glob("[0-9]*-*.prompt.md"),
        ]
        if path.exists()
    ]
    if existing_dispatch_files and not overwrite_prompts:
        raise ValueError(
            "task_dir already contains dispatch files; pass the explicit overwrite option: "
            + ", ".join(path.name for path in existing_dispatch_files)
        )
    if artifact_dir.exists() and any(artifact_dir.glob("*.json")):
        raise ValueError(
            "task_dir already contains artifact JSON files; use a fresh dispatch directory "
            "or finish/repair the existing MAS run before generating a new bundle"
        )
    if overwrite_prompts:
        for path in task_dir.glob("[0-9]*-*.prompt.md"):
            path.unlink()
    bundle = bind_dispatch_identity(bundle)
    bundle_path = task_dir / "mas_task_bundle.json"
    write_json(bundle_path, bundle)

    task_files: list[dict[str, str]] = []
    for index, task in enumerate(bundle.get("tasks", []), start=1):
        if not isinstance(task, dict):
            continue
        task_path = task_dir / task_file_name(index, task)
        write_text(task_path, prompt_markdown(bundle, task))
        task_files.append(
            {
                "role": str(task.get("role") or ""),
                "run_id": str(bundle.get("run_id") or ""),
                "task_id": str(task.get("task_id") or ""),
                "artifact_owner": str(task.get("artifact_owner") or task.get("role") or ""),
                "artifact_type": str(task.get("artifact_type") or ""),
                "artifact_schema": str(task.get("artifact_schema") or ""),
                "dispatch_phase": str(task.get("dispatch_phase") or ""),
                "secondary_artifacts": [str(item) for item in task.get("secondary_artifacts", [])],
                "path": task_path.name,
            }
        )

    manifest = {
        "schema_version": "1.0",
        "run_id": str(bundle.get("run_id") or ""),
        "bundle_file": bundle_path.name,
        "mas_required": bool(bundle.get("mas_required")),
        "task_count": len(task_files),
        "task_files": task_files,
        "dispatch_phases": DISPATCH_PHASES,
        "artifact_collection": {
            "artifact_dir": "artifacts",
            "collector": "scripts/collect_mas_artifacts.py",
            "summary_file": "mas_run_summary.json",
            "combined_artifacts_file": "mas_artifacts_collected.json",
        },
        "artifact_owners": bundle.get("artifact_owners", {}),
        "dispatch_protocol": bundle.get("dispatch_protocol", {}),
        "validation": bundle.get("validation", {}),
    }
    manifest_path = task_dir / "dispatch_manifest.json"
    write_json(manifest_path, manifest)
    return {
        "task_dir": str(task_dir),
        "bundle_file": str(bundle_path),
        "manifest_file": str(manifest_path),
        "task_files": [str(task_dir / item["path"]) for item in task_files],
    }


def write_dispatch_files(
    bundle: dict[str, Any],
    task_dir: Path,
    *,
    overwrite_prompts: bool = False,
) -> dict[str, Any]:
    errors = validate_bundle(bundle, require_material_coverage=True)
    if errors:
        raise ValueError("; ".join(errors))
    with mas_task_lock(task_dir, exclusive=True):
        return _write_dispatch_files_unlocked(
            bundle,
            task_dir,
            overwrite_prompts=overwrite_prompts,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 MAS specialist task bundle")
    parser.add_argument("--request-json", help="包含 run_profile/source_mode/risk_flags 的 JSON 请求")
    parser.add_argument("--run-profile", choices=sorted(RUN_PROFILES), default=None)
    parser.add_argument("--source-mode", choices=sorted(SOURCE_MODES), default=None)
    parser.add_argument("--meeting-type", choices=sorted(MEETING_TYPES), default=None)
    parser.add_argument("--risk", action="append", default=[], help="风险标记，可重复")
    parser.add_argument("--material", action="append", default=[], help="当前会议材料路径，可重复")
    parser.add_argument("--speaker-turn-manifest", help="由 build_speaker_turn_manifest.py 生成的 JSON")
    parser.add_argument("--speaker-editing-mode", choices=sorted(SPEAKER_EDITING_MODES), help="发言人编辑路由：auto/skip/full")
    parser.add_argument("--out", help="写入 JSON 文件；默认输出到 stdout")
    parser.add_argument("--task-dir", help="写入 Codex-ready subagent prompt 文件和 dispatch manifest")
    parser.add_argument("--overwrite-dispatch", action="store_true", help="显式覆盖无 artifact 的已有 dispatch 文件")
    args = parser.parse_args()

    try:
        request: dict[str, Any] = {}
        if args.request_json:
            payload = read_json(Path(args.request_json))
            if not isinstance(payload, dict):
                raise ValueError("request-json 顶层必须是 JSON object")
            request.update(payload)
        if args.run_profile:
            request["run_profile"] = args.run_profile
        if args.source_mode:
            request["source_mode"] = args.source_mode
        if args.meeting_type:
            request["meeting_type"] = args.meeting_type
        if args.risk:
            request["risk_flags"] = normalized_flags(request.get("risk_flags", [])) + normalized_flags(args.risk)
        if args.material:
            existing_materials = request.get("materials", [])
            if not isinstance(existing_materials, list):
                raise ValueError("materials 必须是 JSON array")
            request["materials"] = [*existing_materials, *args.material]
        if args.speaker_turn_manifest:
            request["speaker_turn_manifest"] = read_json(Path(args.speaker_turn_manifest))
        if args.speaker_editing_mode:
            request["speaker_editing_mode"] = args.speaker_editing_mode

        bundle = build_bundle_from_request(request)
        errors = validate_bundle(
            bundle,
            require_material_coverage=bool(args.request_json or args.material or args.task_dir),
        )
        if errors:
            print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
            return 1
        dispatch_files: dict[str, Any] | None = None
        if args.task_dir:
            dispatch_files = write_dispatch_files(
                bundle,
                Path(args.task_dir),
                overwrite_prompts=bool(args.overwrite_dispatch),
            )
            bound_bundle = read_json(Path(dispatch_files["bundle_file"]))
            if not isinstance(bound_bundle, dict):
                raise ValueError("写入后的 MAS task bundle 顶层必须是 JSON object")
            bundle = bound_bundle
        output_payload = dict(bundle)
        if dispatch_files:
            output_payload["dispatch_files"] = dispatch_files
        if args.out:
            write_json(Path(args.out), output_payload)
        if not args.out:
            print(json.dumps(output_payload, ensure_ascii=False, indent=2))
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(json.dumps({"ok": False, "errors": [f"MAS task bundle 生成失败: {exc}"]}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
