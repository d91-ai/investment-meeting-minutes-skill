#!/usr/bin/env python3
"""Deterministically assemble fidelity-review shards.

The fidelity reviewer is a specialist and owns only one bounded shard.  This
module is deliberately main-owned: it reads the bound ``fidelity_diff_manifest``
and dispatch metadata, checks that every review group/span is covered exactly
once, and writes one canonical ``fidelity_review`` plus an assembly receipt.
It never writes Markdown, a sidecar, or any other final-note material.

The manifest and shard vocabulary is intentionally small and stable.  A few
read-only aliases are accepted so that a bundle produced during the schema
migration can still be assembled (``groups``/``spans`` and
``reviewed_span_ids`` are the only aliases).  No semantic finding is selected
over another one; conflicts and unresolved findings remain in the merged
artifact in deterministic source/group/shard order.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from collect_mas_artifacts import collect_artifact_files, merge_artifact_files
from mas_task_lock import mas_task_lock
from validate_mas_artifacts import canonical_json_digest


FIDELITY_SHARD_PREFIX = "fidelity_review_shard__"
MAIN_OUTPUT_TYPES = ("fidelity_review", "fidelity_review_assembly_receipt")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_ARTIFACT = re.compile(r"^fidelity_review_shard__shard_[0-9]{3,}$")
PHASE = "draft_review"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Write a JSON file with fsync + replace, never exposing a partial file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _hash(value: Any, field: str, *, required: bool = True) -> str:
    result = _text(value).lower()
    if not result and not required:
        return ""
    if not HEX_SHA256.fullmatch(result):
        raise ValueError(f"{field} 必须是 64 位小写 SHA-256")
    return result


def _list(value: Any, field: str, *, allow_empty: bool = False) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是 JSON array")
    if not allow_empty and not value:
        raise ValueError(f"{field} 不得为空")
    return value


def _unique_strings(values: list[Any], field: str, *, allow_empty: bool = False) -> list[str]:
    result = [_text(value) for value in values]
    if any(not value for value in result):
        raise ValueError(f"{field} 不得包含空值")
    if len(result) != len(set(result)):
        raise ValueError(f"{field} 不得重复")
    if not result and not allow_empty:
        raise ValueError(f"{field} 不得为空")
    return result


def _manifest_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    value = bundle.get("fidelity_diff_manifest")
    if value in (None, ""):
        # This alias was used by an early draft of the contract.  It is kept
        # only as an input compatibility shim; emitted artifacts use the
        # canonical fidelity_diff_manifest binding.
        value = bundle.get("fidelity_review_manifest")
    if not isinstance(value, dict):
        raise ValueError("当前 MAS bundle 未绑定 fidelity_diff_manifest")
    manifest = dict(value)
    manifest_hash = _hash(manifest.get("manifest_sha256"), "fidelity_diff_manifest.manifest_sha256")
    expected_hash = canonical_json_digest(
        {
            key: item
            for key, item in manifest.items()
            if key
            not in {
                "shards",
                "shard_count",
                "shard_artifact_types",
                "manifest_sha256",
                "deterministic_hash",
            }
        }
    )
    if manifest_hash != expected_hash:
        raise ValueError("fidelity_diff_manifest.manifest_sha256 与内容不一致")
    if "fidelity_diff_manifest_sha256" in bundle and _text(bundle.get("fidelity_diff_manifest_sha256")):
        if _hash(bundle.get("fidelity_diff_manifest_sha256"), "bundle.fidelity_diff_manifest_sha256") != manifest_hash:
            raise ValueError("bundle.fidelity_diff_manifest_sha256 与 manifest 不一致")
    return manifest


def _span_id(span: Any, field: str) -> str:
    if isinstance(span, dict):
        value = span.get("span_id") or span.get("id")
    else:
        value = span
    result = _text(value)
    if not result:
        raise ValueError(f"{field} span_id 不得为空")
    return result


def _span_hash(span: dict[str, Any], keys: tuple[str, ...], default: str, field: str) -> str:
    for key in keys:
        if _text(span.get(key)):
            return _hash(span.get(key), field)
    return default


def _group_span_records(
    raw_group: dict[str, Any],
    group_id: str,
    source_sha256: str,
    draft_sha256: str,
) -> list[dict[str, Any]]:
    """Return canonical span records for explicit or range-only groups.

    The diff builder intentionally keeps source/final character ranges rather
    than copying prose.  Such a group has no user-facing span id, so the main
    flow derives an opaque, deterministic id from the group and ordinal.  The
    id is only an assembly key; it is never emitted as meeting-note content.
    """

    raw_spans = raw_group.get("span_ids")
    if raw_spans is None:
        raw_spans = raw_group.get("spans")
    if raw_spans is not None:
        if not isinstance(raw_spans, list):
            raise ValueError(f"review_group {group_id}.spans 必须是 JSON array")
        records: list[dict[str, Any]] = []
        for index, raw_span in enumerate(raw_spans, start=1):
            sid = _span_id(raw_span, f"review_group {group_id}")
            if isinstance(raw_span, dict):
                source_span_id = _text(raw_span.get("source_span_id") or raw_span.get("source_id"))
                if not source_span_id:
                    raise ValueError(f"review_group {group_id} span {sid} 缺少 source_span_id")
                records.append(
                    {
                        "span_id": sid,
                        "review_group_id": group_id,
                        "source_span_id": source_span_id,
                        "source_sha256": _span_hash(
                            raw_span,
                            ("source_sha256", "source_span_sha256"),
                            source_sha256,
                            f"review_group {group_id} span {sid}.source_sha256",
                        ),
                        "draft_sha256": _span_hash(
                            raw_span,
                            ("draft_sha256", "final_sha256", "draft_span_sha256"),
                            draft_sha256,
                            f"review_group {group_id} span {sid}.draft_sha256",
                        ),
                    }
                )
            else:
                records.append(
                    {
                        "span_id": sid,
                        "review_group_id": group_id,
                        "source_span_id": f"{group_id}:source:{index:03d}",
                        "source_sha256": source_sha256,
                        "draft_sha256": draft_sha256,
                    }
                )
        return records

    source_ranges = raw_group.get("source_spans")
    draft_ranges = raw_group.get("draft_spans")
    if source_ranges is None and draft_ranges is None:
        source_ranges = [raw_group]
        draft_ranges = [raw_group]
    if not isinstance(source_ranges, list) or not isinstance(draft_ranges, list):
        raise ValueError(f"review_group {group_id}.source_spans/draft_spans 必须是 JSON array")
    if len(source_ranges) != len(draft_ranges) or not source_ranges:
        raise ValueError(f"review_group {group_id}.source_spans 与 draft_spans 必须一一对应且非空")
    records = []
    group_source_hash = _text(raw_group.get("source_span_sha256"))
    group_draft_hash = _text(raw_group.get("draft_span_sha256"))
    for index, (source_range, draft_range) in enumerate(zip(source_ranges, draft_ranges), start=1):
        if not isinstance(source_range, dict) or not isinstance(draft_range, dict):
            raise ValueError(f"review_group {group_id} source/draft span range 必须是 object")
        sid = f"{group_id}:span:{index:03d}"
        source_span_id = _text(source_range.get("source_span_id") or source_range.get("span_id"))
        if not source_span_id:
            source_span_id = f"{group_id}:source:{index:03d}"
        records.append(
            {
                "span_id": sid,
                "review_group_id": group_id,
                "source_span_id": source_span_id,
                "source_sha256": _span_hash(
                    source_range,
                    ("source_sha256", "source_span_sha256", "sha256"),
                    _hash(group_source_hash, f"review_group {group_id}.source_span_sha256", required=False) or source_sha256,
                    f"review_group {group_id} span {sid}.source_sha256",
                ),
                "draft_sha256": _span_hash(
                    draft_range,
                    ("draft_sha256", "draft_span_sha256", "sha256"),
                    _hash(group_draft_hash, f"review_group {group_id}.draft_span_sha256", required=False) or draft_sha256,
                    f"review_group {group_id} span {sid}.draft_sha256",
                ),
                "source_range": dict(source_range),
                "draft_range": dict(draft_range),
            }
        )
    return records


def _normalise_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Validate manifest identity and return ordered groups/spans/shards."""

    source_sha256 = _hash(manifest.get("source_sha256"), "fidelity_diff_manifest.source_sha256")
    draft_sha256 = _hash(manifest.get("draft_sha256"), "fidelity_diff_manifest.draft_sha256")
    scope_sha256 = _hash(manifest.get("scope_sha256"), "fidelity_diff_manifest.scope_sha256", required=False)
    source_manifest_sha256 = _hash(
        manifest.get("source_manifest_sha256"),
        "fidelity_diff_manifest.source_manifest_sha256",
        required=False,
    )
    span_map_sha256 = _hash(
        manifest.get("span_map_sha256"),
        "fidelity_diff_manifest.span_map_sha256",
        required=True,
    )
    mode = _text(manifest.get("mode") or manifest.get("review_mode")).casefold()
    change_status = _text(manifest.get("change_status") or manifest.get("status")).casefold()
    groups_raw = manifest.get("review_groups")
    if groups_raw is None:
        groups_raw = manifest.get("groups")
    if not isinstance(groups_raw, list):
        raise ValueError("fidelity_diff_manifest.review_groups 必须是 JSON array")

    group_ids: list[str] = []
    group_spans: dict[str, list[str]] = {}
    span_meta: dict[str, dict[str, Any]] = {}
    for index, raw_group in enumerate(groups_raw, start=1):
        if not isinstance(raw_group, dict):
            raise ValueError(f"fidelity_diff_manifest.review_groups[{index}] 必须是 object")
        group_id = _text(raw_group.get("group_id") or raw_group.get("review_group_id"))
        if not group_id or group_id in group_spans:
            raise ValueError("fidelity_diff_manifest review_group_id 必须唯一且非空")
        records = _group_span_records(raw_group, group_id, source_sha256, draft_sha256)
        ids = [str(record["span_id"]) for record in records]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError(f"fidelity_diff_manifest review_group {group_id}.span_ids 不得为空或重复")
        group_ids.append(group_id)
        group_spans[group_id] = ids
        for record in records:
            sid = str(record["span_id"])
            if sid in span_meta:
                raise ValueError(f"fidelity_diff_manifest span 重复: {sid}")
            span_meta[sid] = record

    top_spans = manifest.get("spans")
    if top_spans is None:
        top_spans = manifest.get("review_spans")
    if top_spans is not None:
        if not isinstance(top_spans, list):
            raise ValueError("fidelity_diff_manifest.spans 必须是 JSON array")
        top_ids: list[str] = []
        for index, raw_span in enumerate(top_spans, start=1):
            if not isinstance(raw_span, dict):
                raise ValueError(f"fidelity_diff_manifest.spans[{index}] 必须是 object")
            sid = _span_id(raw_span, f"fidelity_diff_manifest.spans[{index}]")
            gid = _text(raw_span.get("review_group_id") or raw_span.get("group_id"))
            if not gid or gid not in group_spans:
                raise ValueError(f"fidelity_diff_manifest span {sid} 的 review_group_id 未知")
            if sid not in group_spans[gid]:
                raise ValueError(f"fidelity_diff_manifest span {sid} 不属于声明的 review_group")
            if sid in top_ids:
                raise ValueError(f"fidelity_diff_manifest.spans 重复: {sid}")
            top_ids.append(sid)
            source_span_id = _text(raw_span.get("source_span_id") or raw_span.get("source_id"))
            if not source_span_id:
                raise ValueError(f"fidelity_diff_manifest span {sid} 缺少 source_span_id")
            span_meta[sid] = {
                "span_id": sid,
                "review_group_id": gid,
                "source_span_id": source_span_id,
                "source_sha256": _span_hash(
                    raw_span,
                    ("source_sha256", "source_span_sha256"),
                    source_sha256,
                    f"fidelity_diff_manifest span {sid}.source_sha256",
                ),
                "draft_sha256": _span_hash(
                    raw_span,
                    ("draft_sha256", "final_sha256", "draft_span_sha256"),
                    draft_sha256,
                    f"fidelity_diff_manifest span {sid}.draft_sha256",
                ),
            }

    span_ids = [sid for gid in group_ids for sid in group_spans[gid]]
    if len(span_ids) != len(set(span_ids)):
        raise ValueError("fidelity_diff_manifest review_groups 之间不得重叠")
    if span_meta and set(span_meta) != set(span_ids):
        raise ValueError("fidelity_diff_manifest.spans 必须恰好覆盖 review_groups.span_ids")

    no_change = (
        mode in {"no_change", "unchanged", "unchanged_no_risk"}
        or change_status in {"no_change", "unchanged", "unchanged_no_risk"}
        or (
            manifest.get("semantic_review_required") is False
            and not group_ids
            and not manifest.get("shards")
        )
    )
    shards_raw = manifest.get("shards")
    if shards_raw is None:
        shards_raw = []
    if not isinstance(shards_raw, list):
        raise ValueError("fidelity_diff_manifest.shards 必须是 JSON array")
    shards: list[dict[str, Any]] = []
    assigned_groups: set[str] = set()
    assigned_spans: set[str] = set()
    artifact_types: set[str] = set()
    for index, raw_shard in enumerate(shards_raw, start=1):
        if not isinstance(raw_shard, dict):
            raise ValueError(f"fidelity_diff_manifest.shards[{index}] 必须是 object")
        shard_id = _text(raw_shard.get("shard_id"))
        artifact_type = _text(raw_shard.get("artifact_type"))
        if not shard_id or not artifact_type or not SAFE_ARTIFACT.fullmatch(artifact_type):
            raise ValueError(f"fidelity_diff_manifest shard {index} shard_id/artifact_type 不合法")
        if artifact_type in artifact_types:
            raise ValueError(f"fidelity_diff_manifest artifact_type 重复: {artifact_type}")
        group_values = raw_shard.get("review_group_ids")
        if group_values is None:
            group_values = raw_shard.get("group_ids")
        if group_values is None and isinstance(raw_shard.get("coverage"), dict):
            group_values = raw_shard["coverage"].get("group_ids")
        group_values = _unique_strings(_list(group_values, f"{artifact_type}.review_group_ids"), f"{artifact_type}.review_group_ids")
        if any(group_id not in group_spans for group_id in group_values):
            raise ValueError(f"{artifact_type}.review_group_ids 含未知 review_group")
        span_values = raw_shard.get("span_ids")
        if span_values is None:
            span_values = raw_shard.get("reviewed_span_ids")
        if span_values is None and isinstance(raw_shard.get("coverage"), dict):
            span_values = raw_shard["coverage"].get("span_ids")
        if span_values is None:
            # Range-only diff manifests assign whole review groups to a shard;
            # the canonical span ids are derived above in group order.
            span_values = [sid for gid in group_ids for sid in group_spans[gid] if gid in group_values]
        expected_shard_spans = [sid for gid in group_ids for sid in group_spans[gid] if gid in group_values]
        actual_shard_spans = _unique_strings(_list(span_values, f"{artifact_type}.span_ids"), f"{artifact_type}.span_ids")
        if actual_shard_spans != expected_shard_spans:
            raise ValueError(f"{artifact_type}.span_ids 必须按 manifest 顺序恰好覆盖完整 review_group")
        if assigned_groups.intersection(group_values):
            raise ValueError("fidelity_diff_manifest review_group 被多个 shard 覆盖")
        if assigned_spans.intersection(actual_shard_spans):
            raise ValueError("fidelity_diff_manifest span 被多个 shard 覆盖")
        assigned_groups.update(group_values)
        assigned_spans.update(actual_shard_spans)
        shard_core = {
            "shard_id": shard_id,
            "shard_number": raw_shard.get("shard_number"),
            "group_ids": group_values,
            "group_count": raw_shard.get("group_count", len(group_values)),
            "span_ids": actual_shard_spans,
            "source_sha256": source_sha256,
            "draft_sha256": draft_sha256,
            "span_map_sha256": span_map_sha256,
            "manifest_sha256": _hash(manifest.get("manifest_sha256"), "manifest_sha256"),
        }
        expected_shard_hash = canonical_json_digest(shard_core)
        raw_input_hash = _text(raw_shard.get("input_sha256")) or _text(raw_shard.get("shard_sha256"))
        if _hash(raw_input_hash, f"{artifact_type}.input_sha256") != expected_shard_hash:
            raise ValueError(f"{artifact_type}.input_sha256 与 shard core 内容不一致")
        shard_hash = _text(raw_shard.get("shard_sha256")) or expected_shard_hash
        if _hash(shard_hash, f"{artifact_type}.shard_sha256") != expected_shard_hash:
            raise ValueError(f"{artifact_type}.shard_sha256 与 shard core 内容不一致")
        shards.append(
            {
                **raw_shard,
                "shard_id": shard_id,
                "artifact_type": artifact_type,
                "review_group_ids": group_values,
                "span_ids": actual_shard_spans,
                "shard_sha256": shard_hash,
                "input_sha256": _text(raw_shard.get("input_sha256")),
                "span_map_sha256": _text(raw_shard.get("span_map_sha256")) or span_map_sha256,
                "group_count": raw_shard.get("group_count", len(group_values)),
                "shard_number": raw_shard.get("shard_number"),
                "task_id": _text(raw_shard.get("task_id")),
                "dispatch_phase": _text(raw_shard.get("dispatch_phase") or PHASE),
                "artifact_owner": _text(raw_shard.get("artifact_owner") or "Fidelity Reviewer"),
            }
        )
        artifact_types.add(artifact_type)

    if no_change:
        if group_ids or shards:
            raise ValueError("no-change fidelity_diff_manifest 不得包含 review_groups/shards")
    else:
        if not group_ids:
            raise ValueError("有变化的 fidelity_diff_manifest 必须包含 review_groups")
        if not shards:
            raise ValueError("有变化的 fidelity_diff_manifest 必须包含 shards")
        if assigned_groups != set(group_ids) or assigned_spans != set(span_ids):
            raise ValueError("fidelity_diff_manifest.shards 必须恰好覆盖全部 review_groups/spans")
    all_groups = manifest.get("groups") if isinstance(manifest.get("groups"), list) else review_groups
    all_span_count = sum(
        len(group.get("span_ids", []))
        for group in all_groups
        if isinstance(group, dict)
    )
    if manifest.get("group_count") is not None and manifest.get("group_count") != len(all_groups):
        raise ValueError("fidelity_diff_manifest.group_count 与内容不一致")
    if manifest.get("span_count") is not None and manifest.get("span_count") != all_span_count:
        raise ValueError("fidelity_diff_manifest.span_count 与内容不一致")
    if manifest.get("shard_count") is not None and manifest.get("shard_count") != len(shards):
        raise ValueError("fidelity_diff_manifest.shard_count 与内容不一致")
    return {
        "manifest": manifest,
        "manifest_sha256": _hash(manifest.get("manifest_sha256"), "manifest_sha256"),
        "source_sha256": source_sha256,
        "draft_sha256": draft_sha256,
        "scope_sha256": scope_sha256,
        "source_manifest_sha256": source_manifest_sha256,
        "span_map_sha256": span_map_sha256,
        "mode": "no_change" if no_change else (mode or ("single" if len(shards) == 1 else "parallel")),
        "group_ids": group_ids,
        "group_spans": group_spans,
        "span_ids": span_ids,
        "span_meta": span_meta,
        "shards": shards,
        "no_change": no_change,
    }


def _dispatch_records(bundle: dict[str, Any], dispatch_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    bundle_run_id = _text(bundle.get("run_id"))
    if not bundle_run_id:
        raise ValueError("MAS bundle 缺少 run_id")
    if _text(dispatch_manifest.get("run_id")) != bundle_run_id:
        raise ValueError("MAS bundle/dispatch_manifest run_id 不一致")
    bundle_tasks = {
        _text(item.get("task_id")): item
        for item in bundle.get("tasks", [])
        if isinstance(item, dict) and _text(item.get("task_id"))
    }
    manifest_tasks = {
        _text(item.get("task_id")): item
        for item in dispatch_manifest.get("task_files", [])
        if isinstance(item, dict) and _text(item.get("task_id"))
    }
    records: dict[str, dict[str, Any]] = {}
    for task_id, task in bundle_tasks.items():
        if task_id in manifest_tasks:
            records[task_id] = {"bundle": task, "manifest": manifest_tasks[task_id]}
    return records


def _validate_shard_dispatch(
    task_dir: Path,
    bundle: dict[str, Any],
    dispatch_manifest: dict[str, Any],
    spec: dict[str, Any],
    artifact: dict[str, Any],
    source_payload: dict[str, Any],
    info: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    artifact_type = spec["artifact_type"]
    run_id = _text(bundle.get("run_id"))
    root_fields = {
        "run_id": _text(source_payload.get("run_id")),
        "task_id": _text(source_payload.get("task_id")),
        "dispatch_phase": _text(source_payload.get("dispatch_phase")),
        "artifact_owner": _text(source_payload.get("artifact_owner")),
        "artifact_type": _text(source_payload.get("artifact_type")),
    }
    if root_fields["artifact_type"] != artifact_type:
        errors.append(f"{artifact_type} artifact file identity 中 artifact_type 不匹配")
    if root_fields["run_id"] != run_id:
        errors.append(f"{artifact_type} run_id 不匹配")
    matching_records = [
        (task_id, record)
        for task_id, record in records.items()
        if _text(record.get("bundle", {}).get("artifact_type")) == artifact_type
    ]
    if len(matching_records) != 1:
        errors.append(f"{artifact_type} 未在 bundle/dispatch_manifest 中唯一匹配")
        task_id = root_fields["task_id"]
        record = None
    else:
        task_id, record = matching_records[0]
    if not task_id or root_fields["task_id"] != task_id:
        errors.append(f"{artifact_type} task_id 与可信 dispatch 不匹配")
    expected_phase = _text((record or {}).get("bundle", {}).get("dispatch_phase")) or PHASE
    if root_fields["dispatch_phase"] != expected_phase:
        errors.append(f"{artifact_type} dispatch_phase 必须为 {expected_phase}")
    expected_owner = _text(
        (record or {}).get("bundle", {}).get("artifact_owner")
        or (record or {}).get("bundle", {}).get("role")
    ) or "Fidelity Reviewer"
    if root_fields["artifact_owner"] != expected_owner:
        errors.append(f"{artifact_type} artifact_owner 不匹配")
    if record is None:
        errors.append(f"{artifact_type} task identity 无可信 dispatch record")
    else:
        for source_name, task in record.items():
            if _text(task.get("run_id")) != run_id:
                errors.append(f"{artifact_type} {source_name}.run_id 不匹配")
            if _text(task.get("artifact_type")) != artifact_type:
                errors.append(f"{artifact_type} {source_name}.artifact_type 不匹配")
            if _text(task.get("dispatch_phase")) != expected_phase:
                errors.append(f"{artifact_type} {source_name}.dispatch_phase 不匹配")
            owner = _text(task.get("artifact_owner") or task.get("role"))
            if owner != expected_owner:
                errors.append(f"{artifact_type} {source_name}.artifact_owner 不匹配")
    expected_hashes = {
        "manifest_sha256": info["manifest_sha256"],
        "source_sha256": info["source_sha256"],
        "draft_sha256": info["draft_sha256"],
        "shard_sha256": spec["shard_sha256"],
        "shard_id": spec["shard_id"],
    }
    for field, expected in expected_hashes.items():
        if _text(artifact.get(field)) != expected:
            # input_sha256 is the legacy name for the deterministic shard
            # input digest; accept it only when it is exactly the same value.
            if field == "shard_sha256" and _text(artifact.get("input_sha256")) == expected:
                continue
            errors.append(f"{artifact_type}.{field} 与 manifest/shard spec 不一致")
    # span_map/scope are manifest-level bindings.  New shard envelopes may
    # carry them; older envelopes do not, so absence remains compatible while
    # a supplied value is still checked strictly.
    for field, expected in (("span_map_sha256", info.get("span_map_sha256")), ("scope_sha256", info.get("scope_sha256"))):
        if expected and _text(artifact.get(field)) and _text(artifact.get(field)) != expected:
            errors.append(f"{artifact_type}.{field} 与 manifest 不一致")
    expected_groups = spec["review_group_ids"]
    expected_spans = spec["span_ids"]
    actual_groups = artifact.get("review_group_ids")
    actual_spans = artifact.get("span_ids")
    if actual_groups is None:
        actual_groups = artifact.get("group_ids")
    if actual_spans is None:
        actual_spans = artifact.get("reviewed_span_ids")
    try:
        if _unique_strings(_list(actual_groups, f"{artifact_type}.review_group_ids"), f"{artifact_type}.review_group_ids") != expected_groups:
            errors.append(f"{artifact_type}.review_group_ids 覆盖不一致")
    except ValueError as exc:
        errors.append(str(exc))
    coverage = artifact.get("coverage")
    if coverage is not None:
        if not isinstance(coverage, dict):
            errors.append(f"{artifact_type}.coverage 必须是 object")
        else:
            try:
                coverage_groups = _unique_strings(
                    _list(coverage.get("group_ids"), f"{artifact_type}.coverage.group_ids"),
                    f"{artifact_type}.coverage.group_ids",
                )
                if coverage_groups != expected_groups:
                    errors.append(f"{artifact_type}.coverage.group_ids 覆盖不一致")
            except ValueError as exc:
                errors.append(str(exc))
    try:
        if _unique_strings(_list(actual_spans, f"{artifact_type}.span_ids"), f"{artifact_type}.span_ids") != expected_spans:
            errors.append(f"{artifact_type}.span_ids 覆盖不一致")
    except ValueError as exc:
        errors.append(str(exc))
    status = _text(artifact.get("status")).casefold()
    if status not in {"complete", "blocked"}:
        errors.append(f"{artifact_type}.status 必须为 complete 或 blocked")
    for collection_name in ("findings", "unresolved_items", "evidence_mappings"):
        value = artifact.get(collection_name)
        if value is None:
            continue
        if not isinstance(value, list):
            errors.append(f"{artifact_type}.{collection_name} 必须是 JSON array")
            continue
        for index, item in enumerate(value, start=1):
            if isinstance(item, str):
                continue
            if not isinstance(item, dict):
                errors.append(f"{artifact_type}.{collection_name}[{index}] 必须是 object 或 string")
                continue
            item_span_id = _text(item.get("span_id") or item.get("reviewed_span_id"))
            if item_span_id and item_span_id not in expected_spans:
                errors.append(f"{artifact_type}.{collection_name}[{index}] 含越界 span_id")
            if collection_name == "evidence_mappings":
                if not item_span_id:
                    errors.append(f"{artifact_type}.evidence_mappings[{index}] 缺少 span_id")
                source_span_id = _text(item.get("source_span_id") or item.get("source_id"))
                expected_source_span = info["span_meta"].get(item_span_id, {}).get("source_span_id")
                if source_span_id != expected_source_span:
                    errors.append(f"{artifact_type}.evidence_mappings[{index}] source_span_id 不匹配")
                for hash_field, expected in (("source_sha256", info["span_meta"].get(item_span_id, {}).get("source_sha256")), ("draft_sha256", info["span_meta"].get(item_span_id, {}).get("draft_sha256"))):
                    if _text(item.get(hash_field)) and expected and _text(item.get(hash_field)) != expected:
                        errors.append(f"{artifact_type}.evidence_mappings[{index}].{hash_field} 不匹配")
                if not (_text(item.get("evidence_path")) or _text(item.get("evidence") or item.get("evidence_text"))):
                    errors.append(f"{artifact_type}.evidence_mappings[{index}] 缺少 evidence_path/evidence")
    return errors


def _merge_findings(shard_artifacts: list[tuple[dict[str, Any], dict[str, Any]]], info: dict[str, Any]) -> dict[str, Any]:
    findings: list[Any] = []
    unresolved: list[Any] = []
    evidence: list[Any] = []
    conflicts: list[Any] = []
    source_mapping_failures: list[Any] = []
    summary_compression_findings: list[Any] = []
    pronoun_rewrite_findings: list[Any] = []
    omission_findings: list[Any] = []
    recommended_revisions: list[Any] = []
    statuses: list[str] = []
    group_ids: list[str] = []
    span_ids: list[str] = []
    for spec, artifact in shard_artifacts:
        statuses.append(_text(artifact.get("status")).casefold())
        group_ids.extend(spec["review_group_ids"])
        span_ids.extend(spec["span_ids"])
        for target, field_names in (
            (findings, ("findings", "review_findings")),
            (unresolved, ("unresolved_items", "unresolved")),
            (evidence, ("evidence_mappings", "evidence")),
            (conflicts, ("conflicts",)),
            (source_mapping_failures, ("source_mapping_failures",)),
            (summary_compression_findings, ("summary_compression_findings",)),
            (pronoun_rewrite_findings, ("pronoun_rewrite_findings",)),
            (omission_findings, ("omission_findings",)),
            (recommended_revisions, ("recommended_revisions",)),
        ):
            for field_name in field_names:
                value = artifact.get(field_name)
                if isinstance(value, list):
                    target.extend(value)
                    break
    statuses = statuses or ["complete"]
    return {
        "schema_version": "1.0",
        "manifest_sha256": info["manifest_sha256"],
        "source_sha256": info["source_sha256"],
        "draft_sha256": info["draft_sha256"],
        **({"scope_sha256": info["scope_sha256"]} if info.get("scope_sha256") else {}),
        "mode": info["mode"],
        "status": "blocked" if "blocked" in statuses else "complete",
        "review_group_ids": group_ids,
        "reviewed_span_ids": span_ids,
        "paragraphs_reviewed": len(span_ids),
        "source_mapping_failures": source_mapping_failures,
        "summary_compression_findings": summary_compression_findings,
        "pronoun_rewrite_findings": pronoun_rewrite_findings,
        "omission_findings": omission_findings,
        "recommended_revisions": recommended_revisions,
        "findings": findings,
        "unresolved_items": unresolved,
        "conflicts": conflicts,
        "evidence_mappings": evidence,
        "bindings": {
            "manifest_sha256": info["manifest_sha256"],
            "source_sha256": info["source_sha256"],
            "draft_sha256": info["draft_sha256"],
            **({"span_map_sha256": info["span_map_sha256"]} if info.get("span_map_sha256") else {}),
            **({"scope_sha256": info["scope_sha256"]} if info.get("scope_sha256") else {}),
        },
    }


def _empty_review(info: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "manifest_sha256": info["manifest_sha256"],
        "source_sha256": info["source_sha256"],
        "draft_sha256": info["draft_sha256"],
        **({"scope_sha256": info["scope_sha256"]} if info.get("scope_sha256") else {}),
        "mode": "no_change",
        "status": "no_change",
        "review_group_ids": [],
        "reviewed_span_ids": [],
        "paragraphs_reviewed": 0,
        "source_mapping_failures": [],
        "summary_compression_findings": [],
        "pronoun_rewrite_findings": [],
        "omission_findings": [],
        "recommended_revisions": [],
        "findings": [],
        "unresolved_items": [],
        "conflicts": [],
        "evidence_mappings": [],
        "bindings": {
            "manifest_sha256": info["manifest_sha256"],
            "source_sha256": info["source_sha256"],
            "draft_sha256": info["draft_sha256"],
            **({"span_map_sha256": info["span_map_sha256"]} if info.get("span_map_sha256") else {}),
            **({"scope_sha256": info["scope_sha256"]} if info.get("scope_sha256") else {}),
        },
    }


def _envelope(run_id: str, artifact_type: str, artifact: Any) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task_id": f"{run_id}:main:{artifact_type}",
        "dispatch_phase": PHASE,
        "artifact_owner": "Main Orchestrator",
        "artifact_type": artifact_type,
        "artifact": artifact,
    }


def _archive_existing(paths: list[Path], task_dir: Path) -> list[str]:
    archive_dir = task_dir / "repair_history" / "superseded"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived: list[str] = []
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    for index, path in enumerate(paths, start=1):
        target = archive_dir / f"{stamp}.{time.time_ns()}.{index}.{path.name}"
        os.replace(path, target)
        archived.append(str(target))
    return archived


def assemble_fidelity_review_shards(task_dir: Path, *, replace: bool = False) -> dict[str, Any]:
    """Assemble one deterministic fidelity review for a MAS task directory."""

    task_dir = task_dir.expanduser()
    artifact_dir = task_dir / "artifacts"
    output_paths = {name: artifact_dir / f"{name}.json" for name in MAIN_OUTPUT_TYPES}
    with mas_task_lock(task_dir, exclusive=True):
        bundle_path = task_dir / "mas_task_bundle.json"
        dispatch_path = task_dir / "dispatch_manifest.json"
        bundle = read_json(bundle_path)
        dispatch_manifest = read_json(dispatch_path)
        if not isinstance(bundle, dict) or not isinstance(dispatch_manifest, dict):
            raise ValueError("MAS bundle/dispatch_manifest 顶层必须是 JSON object")
        info = _normalise_manifest(_manifest_from_bundle(bundle))
        records = _dispatch_records(bundle, dispatch_manifest)
        artifacts, artifact_sources, merge_errors, duplicates = merge_artifact_files(
            collect_artifact_files(artifact_dir)
        )
        errors = [str(error) for error in merge_errors]
        if duplicates:
            errors.append("存在重复 MAS artifact，不能组装 fidelity review")
        source_by_type = {
            _text(item.get("artifact_type")): Path(_text(item.get("path")))
            for item in artifact_sources
            if isinstance(item, dict) and _text(item.get("artifact_type"))
        }
        dynamic_types = {
            _text(artifact_type)
            for artifact_type in artifacts
            if _text(artifact_type).startswith(FIDELITY_SHARD_PREFIX)
        }
        expected_types = {spec["artifact_type"] for spec in info["shards"]}
        if info["no_change"]:
            if dynamic_types:
                errors.append("no-change fidelity manifest 不得存在 specialist fidelity shard artifact")
            shard_artifacts: list[tuple[dict[str, Any], dict[str, Any]]] = []
        else:
            missing = sorted(expected_types - dynamic_types)
            unexpected = sorted(dynamic_types - expected_types)
            if missing:
                errors.append("缺少 fidelity review shard: " + ", ".join(missing))
            if unexpected:
                errors.append("存在未由 fidelity_diff_manifest 授权的 shard: " + ", ".join(unexpected))
            shard_artifacts = []
            for spec in info["shards"]:
                artifact_type = spec["artifact_type"]
                artifact = artifacts.get(artifact_type)
                source_path = source_by_type.get(artifact_type)
                if not isinstance(artifact, dict) or source_path is None:
                    continue
                if source_path.name != f"{artifact_type}.json":
                    errors.append(f"{artifact_type} artifact file identity 与文件名不一致")
                try:
                    source_payload = read_json(source_path)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    errors.append(f"无法读取 {artifact_type} artifact file identity: {exc}")
                    continue
                if not isinstance(source_payload, dict):
                    errors.append(f"{artifact_type} artifact envelope 必须是 object")
                    continue
                errors.extend(
                    _validate_shard_dispatch(
                        task_dir,
                        bundle,
                        dispatch_manifest,
                        spec,
                        artifact,
                        source_payload,
                        info,
                        records,
                    )
                )
                shard_artifacts.append((spec, artifact))
            if len(shard_artifacts) != len(info["shards"]):
                errors.append("fidelity review shard 未能完整载入")
        if errors:
            raise ValueError("; ".join(dict.fromkeys(errors)))

        review = _empty_review(info) if info["no_change"] else _merge_findings(shard_artifacts, info)
        shard_digest = canonical_json_digest(
            {
                spec["artifact_type"]: artifact
                for spec, artifact in sorted(shard_artifacts, key=lambda pair: pair[0]["artifact_type"])
            }
        )
        receipt = {
            "schema_version": "1.0",
            "manifest_sha256": info["manifest_sha256"],
            "source_sha256": info["source_sha256"],
            "draft_sha256": info["draft_sha256"],
            **({"span_map_sha256": info["span_map_sha256"]} if info.get("span_map_sha256") else {}),
            **({"scope_sha256": info["scope_sha256"]} if info.get("scope_sha256") else {}),
            "shard_artifact_digest": shard_digest,
            "fidelity_review_sha256": canonical_json_digest(review),
            "review_group_ids": list(info["group_ids"]),
            "span_ids": list(info["span_ids"]),
            "status": "assembled",
            "mode": info["mode"],
            "specialist_artifact_types": [spec["artifact_type"] for spec, _ in shard_artifacts],
        }
        outputs = {
            "fidelity_review": _envelope(_text(bundle.get("run_id")), "fidelity_review", review),
            "fidelity_review_assembly_receipt": _envelope(
                _text(bundle.get("run_id")), "fidelity_review_assembly_receipt", receipt
            ),
        }
        existing = [path for path in output_paths.values() if path.exists()]
        archived: list[str] = []
        if existing:
            if not replace:
                raise ValueError(
                    "fidelity review assembly artifact 已存在；显式传入 --replace 后才能替换: "
                    + ", ".join(str(path) for path in existing)
                )
            archived = _archive_existing(existing, task_dir)
        for artifact_type in MAIN_OUTPUT_TYPES:
            _write_json_atomic(output_paths[artifact_type], outputs[artifact_type])
        return {
            "ok": True,
            "mode": info["mode"],
            "run_id": _text(bundle.get("run_id")),
            "manifest_sha256": info["manifest_sha256"],
            "review_group_count": len(info["group_ids"]),
            "span_count": len(info["span_ids"]),
            "shard_count": len(info["shards"]),
            "status": review.get("status"),
            "archived": archived,
            "artifacts": {name: str(path) for name, path in output_paths.items()},
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="确定性组装 fidelity review shard")
    parser.add_argument("task_dir", type=Path, help="MAS dispatch 目录")
    parser.add_argument("--replace", action="store_true", help="归档并替换已有主流程 fidelity artifacts")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    try:
        result = assemble_fidelity_review_shards(args.task_dir, replace=args.replace)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        result = {"ok": False, "errors": [str(exc)]}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("ok"):
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result["errors"][0], file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
