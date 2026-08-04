#!/usr/bin/env python3
"""Assemble validated entity-verification shards in manifest order.

The entity verifier owns only its shard artifact.  This module is the main
workflow's deterministic assembly boundary: it validates the complete shard
set, applies group/identity conflict propagation, and writes the three
main-owned artifacts used by the pre-draft collector gate.  It deliberately
does not create Markdown or a verification sidecar.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from collect_mas_artifacts import (
    PHASE_ORDER,
    artifact_context_errors,
    collect_artifact_files,
    merge_artifact_files,
)
from mas_task_lock import mas_task_lock
from validate_mas_artifacts import (
    ENTITY_VERIFICATION_SHARD_PREFIX,
    artifact_mapping,
    canonical_json_digest,
    validate_dispatch_identity,
    validate_payload,
)


MAIN_OUTPUT_TYPES = (
    "entity_verification_report",
    "doubtful_items",
    "entity_verification_assembly_receipt",
)
def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    """Write one UTF-8 JSON artifact without exposing a partial file."""

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


def _candidate_term(candidate: dict[str, Any], result: dict[str, Any]) -> str:
    for value in (
        candidate.get("term"),
        candidate.get("candidate_term"),
        candidate.get("original_term"),
        result.get("input_term"),
    ):
        shown = _text(value)
        if shown:
            return shown
    return ""


def _verification_kinds(candidate: dict[str, Any]) -> set[str]:
    values: list[Any] = []
    for field in ("verification_kinds", "relation_kind", "relation_kinds"):
        value = candidate.get(field)
        if isinstance(value, list):
            values.extend(value)
        elif value not in (None, ""):
            values.append(value)
    return {_text(value).casefold() for value in values if _text(value)}


def _doubtful_type(candidate: dict[str, Any]) -> str:
    kinds = _verification_kinds(candidate)
    if kinds & {"person", "person_name", "human_name", "name"}:
        return "人名"
    if kinds & {"speaker", "speaker_id", "speaker_identity", "speaker_role"}:
        return "说话人身份"
    if kinds & {
        "brand_company",
        "company",
        "company_code",
        "company_identity",
        "entity_identity",
        "stock",
        "stock_code",
        "security",
        "security_code",
    }:
        return "公司或证券标的"
    if kinds & {"industry_term", "industry", "term", "term_identity", "terminology"}:
        return "行业术语"
    if kinds & {"numbers_dates", "number", "numbers", "date", "dates", "time", "times"}:
        return "数字或时间"
    return "其他业务事实"


def _stable_unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


class _UnionFind:
    def __init__(self, values: list[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            # Lexicographic roots make the component identity independent of
            # shard completion order and of Python set iteration order.
            if left_root > right_root:
                left_root, right_root = right_root, left_root
            self.parent[right_root] = left_root


def _manifest_candidates(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    raw_candidates = manifest.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise ValueError("entity_candidate_manifest.candidates 必须是非空 JSON array")
    candidates: list[dict[str, Any]] = []
    candidate_ids: list[str] = []
    for index, raw in enumerate(raw_candidates, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"entity_candidate_manifest.candidates[{index}] 必须是 JSON object")
        candidate_id = _text(raw.get("candidate_id"))
        if not candidate_id:
            raise ValueError(f"entity_candidate_manifest.candidates[{index}].candidate_id 不得为空")
        if candidate_id in candidate_ids:
            raise ValueError(f"entity_candidate_manifest candidate_id 重复: {candidate_id}")
        if not _candidate_term(raw, {}):
            raise ValueError(f"entity_candidate_manifest.candidates[{index}] term 不得为空")
        candidates.append(raw)
        candidate_ids.append(candidate_id)
    return candidates, candidate_ids


def _manifest_groups(
    manifest: dict[str, Any], candidate_ids: list[str]
) -> tuple[dict[str, str], dict[str, list[str]]]:
    raw_groups = manifest.get("groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("entity_candidate_manifest.groups 必须是非空 JSON array")
    candidate_set = set(candidate_ids)
    candidate_to_group: dict[str, str] = {}
    group_to_candidates: dict[str, list[str]] = {}
    for index, raw in enumerate(raw_groups, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"entity_candidate_manifest.groups[{index}] 必须是 JSON object")
        group_id = _text(raw.get("group_id"))
        members = [_text(value) for value in raw.get("candidate_ids", [])]
        if not group_id or not members:
            raise ValueError(f"entity_candidate_manifest.groups[{index}] 缺少 group_id/candidate_ids")
        if len(members) != len(set(members)):
            raise ValueError(f"entity_candidate_manifest group candidate_id 重复: {group_id}")
        if any(member not in candidate_set for member in members):
            raise ValueError(f"entity_candidate_manifest group 含未知 candidate_id: {group_id}")
        if group_id in group_to_candidates:
            raise ValueError(f"entity_candidate_manifest group_id 重复: {group_id}")
        for member in members:
            if member in candidate_to_group:
                raise ValueError(f"entity_candidate_manifest candidate 属于多个 group: {member}")
            candidate_to_group[member] = group_id
        group_to_candidates[group_id] = members
    if set(candidate_to_group) != candidate_set:
        missing = sorted(candidate_set - set(candidate_to_group))
        raise ValueError("entity_candidate_manifest groups 未覆盖 candidates: " + ", ".join(missing))
    return candidate_to_group, group_to_candidates


def _manifest_shards(
    manifest: dict[str, Any], candidate_ids: list[str]
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    raw_shards = manifest.get("shards")
    if not isinstance(raw_shards, list) or not raw_shards:
        raise ValueError("parallel entity_candidate_manifest.shards 必须是非空 JSON array")
    candidate_set = set(candidate_ids)
    expected_types: list[str] = []
    shard_by_type: dict[str, dict[str, Any]] = {}
    assigned: list[str] = []
    for index, raw in enumerate(raw_shards, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"entity_candidate_manifest.shards[{index}] 必须是 JSON object")
        artifact_type = _text(raw.get("artifact_type"))
        shard_id = _text(raw.get("shard_id"))
        members = [_text(value) for value in raw.get("candidate_ids", [])]
        if not artifact_type.startswith(ENTITY_VERIFICATION_SHARD_PREFIX) or not shard_id:
            raise ValueError(f"entity_candidate_manifest.shards[{index}] artifact_type/shard_id 不合法")
        if artifact_type in shard_by_type:
            raise ValueError(f"entity_candidate_manifest shard artifact_type 重复: {artifact_type}")
        if not members or len(members) != len(set(members)):
            raise ValueError(f"{artifact_type}.candidate_ids 必须非空且唯一")
        if any(member not in candidate_set for member in members):
            raise ValueError(f"{artifact_type} 包含未知 candidate_id")
        shard_by_type[artifact_type] = raw
        expected_types.append(artifact_type)
        assigned.extend(members)
    if len(assigned) != len(set(assigned)) or set(assigned) != candidate_set:
        raise ValueError("entity_candidate_manifest.shards 必须恰好覆盖全部 candidates 一次")
    return expected_types, shard_by_type


def _component_conflicts(
    candidates: list[dict[str, Any]],
    candidate_ids: list[str],
    candidate_to_group: dict[str, str],
    group_to_candidates: dict[str, list[str]],
    results_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[str]], list[str]]:
    """Return forced-conflict reasons per candidate and stable report strings."""

    uf = _UnionFind(candidate_ids)
    by_group: dict[str, list[str]] = {
        group_id: list(members) for group_id, members in group_to_candidates.items()
    }
    by_identity: dict[str, list[str]] = {}
    for candidate_id in candidate_ids:
        identity_key = _text(results_by_id[candidate_id].get("identity_key"))
        if identity_key:
            by_identity.setdefault(identity_key, []).append(candidate_id)
    for members in by_group.values():
        for member in members[1:]:
            uf.union(members[0], member)
    for members in by_identity.values():
        for member in members[1:]:
            uf.union(members[0], member)

    components: dict[str, list[str]] = {}
    order = {candidate_id: index for index, candidate_id in enumerate(candidate_ids)}
    for candidate_id in candidate_ids:
        components.setdefault(uf.find(candidate_id), []).append(candidate_id)
    for members in components.values():
        members.sort(key=lambda value: order[value])

    reasons_by_id: dict[str, list[str]] = {}
    report_conflicts: list[tuple[int, str]] = []
    for members in sorted(components.values(), key=lambda values: order[values[0]]):
        statuses = {_text(results_by_id[item].get("status")) for item in members}
        canonical_names = {
            _text(results_by_id[item].get("canonical_name"))
            for item in members
            if _text(results_by_id[item].get("canonical_name"))
        }
        conflict_codes = _stable_unique(
            [
                _text(code)
                for item in members
                for code in results_by_id[item].get("conflict_codes", [])
                if _text(code)
            ]
        )
        reasons: list[str] = []
        if statuses == {"confirmed", "unresolved"}:
            reasons.append("status_conflict")
        if len(canonical_names) > 1:
            reasons.append("canonical_name_conflict")
        if conflict_codes:
            reasons.append("conflict_codes=" + ",".join(conflict_codes))
        if not reasons:
            continue
        terms = [
            _candidate_term(
                next(candidate for candidate in candidates if _text(candidate.get("candidate_id")) == item),
                results_by_id[item],
            )
            for item in members
        ]
        detail = "; ".join(reasons) + ": " + ", ".join(terms)
        report_conflicts.append((order[members[0]], detail))
        for item in members:
            reasons_by_id[item] = list(reasons)
    return reasons_by_id, [detail for _, detail in sorted(report_conflicts, key=lambda item: item[0])]


def _doubtful_item(
    candidate: dict[str, Any],
    result: dict[str, Any],
    group_id: str,
    forced_reasons: list[str],
    component_results: list[dict[str, Any]],
) -> dict[str, Any]:
    term = _candidate_term(candidate, result)
    evidence_paths = _stable_unique([_text(value) for value in result.get("evidence_paths", []) if _text(value)])
    canonical_names = _stable_unique(
        [_text(item.get("canonical_name")) for item in component_results if _text(item.get("canonical_name"))]
    )
    identity_keys = _stable_unique(
        [_text(item.get("identity_key")) for item in component_results if _text(item.get("identity_key"))]
    )
    options = canonical_names or identity_keys or ["未形成唯一公开候选"]
    reason = _text(result.get("unresolved_reason")) or "未能确认唯一名称、代码或术语指向"
    if forced_reasons:
        reason = "assembly conflict (" + ", ".join(forced_reasons) + "): " + reason
    evidence_text = ", ".join(evidence_paths) if evidence_paths else reason
    return {
        "原始表述": term,
        "存疑类型": _doubtful_type(candidate),
        "当前判断": "名称、代码或术语候选存在身份冲突" if forced_reasons else "未能确认唯一名称、代码或术语指向",
        "候选项": "; ".join(options),
        "是否需要 sidecar": _doubtful_type(candidate) not in {"人名", "说话人身份"},
        "上下文依据": f"实体核验 manifest group={group_id}",
        "检索/证据路径": evidence_text,
        "最终处理": "保留原始表述并交由主流程标记存疑",
    }


def _envelope(run_id: str, artifact_type: str, artifact: Any) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task_id": f"{run_id}:main:{artifact_type}",
        "dispatch_phase": "pre_draft",
        "artifact_owner": "Main Orchestrator",
        "artifact_type": artifact_type,
        "artifact": artifact,
    }


def assemble_entity_verification_shards(
    task_dir: Path,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    """Assemble all parallel entity shards and write main-owned artifacts."""

    task_dir = task_dir.expanduser()
    artifact_dir = task_dir / "artifacts"
    output_paths = {
        artifact_type: artifact_dir / f"{artifact_type}.json"
        for artifact_type in MAIN_OUTPUT_TYPES
    }
    with mas_task_lock(task_dir, exclusive=True):
        bundle_path = task_dir / "mas_task_bundle.json"
        dispatch_manifest_path = task_dir / "dispatch_manifest.json"
        bundle = read_json(bundle_path)
        if not isinstance(bundle, dict):
            raise ValueError("MAS task bundle 顶层必须是 JSON object")
        manifest = bundle.get("entity_candidate_manifest")
        if not isinstance(manifest, dict):
            raise ValueError("当前 MAS run 未绑定 entity_candidate_manifest")
        if _text(manifest.get("mode")) != "parallel":
            raise ValueError("assemble_entity_verification_shards 仅接受 parallel entity manifest")
        entity_config = bundle.get("entity_verification")
        if isinstance(entity_config, dict) and _text(entity_config.get("effective_mode")) not in {"", "parallel"}:
            raise ValueError("bundle.entity_verification.effective_mode 不是 parallel")
        run_id = _text(bundle.get("run_id"))
        manifest_sha256 = _text(manifest.get("manifest_sha256"))
        if not run_id or len(manifest_sha256) != 64:
            raise ValueError("当前 MAS bundle 缺少有效 run_id/manifest_sha256")

        candidates, candidate_ids = _manifest_candidates(manifest)
        candidate_to_group, group_to_candidates = _manifest_groups(manifest, candidate_ids)
        expected_types, shard_specs = _manifest_shards(manifest, candidate_ids)

        artifacts, artifact_sources, merge_errors, duplicates = merge_artifact_files(
            collect_artifact_files(artifact_dir)
        )
        errors = [str(error) for error in merge_errors]
        if duplicates:
            errors.append("存在重复 MAS artifact，不能组装实体核验")
        actual_dynamic_types = {
            str(artifact_type)
            for artifact_type in artifacts
            if str(artifact_type).startswith(ENTITY_VERIFICATION_SHARD_PREFIX)
        }
        missing = sorted(set(expected_types) - actual_dynamic_types)
        unexpected = sorted(actual_dynamic_types - set(expected_types))
        if missing:
            errors.append("缺少 entity verification shard: " + ", ".join(missing))
        if unexpected:
            errors.append("存在未由 entity_candidate_manifest 授权的 shard artifact: " + ", ".join(unexpected))
        shard_artifacts = {
            artifact_type: artifacts[artifact_type]
            for artifact_type in expected_types
            if artifact_type in artifacts
        }

        # Validate the shard payload shape/evidence before any assembly.  The
        # context gate below binds each shard to manifest hashes and coverage.
        shard_validation = validate_payload(
            {"artifacts": shard_artifacts}, required_artifacts=expected_types
        )
        errors.extend(str(error) for error in shard_validation.get("errors", []))
        for artifact_type, artifact in shard_artifacts.items():
            if isinstance(artifact, dict) and artifact.get("status") != "complete":
                errors.append(f"{artifact_type}.status 必须为 complete 才能汇总")

        if dispatch_manifest_path.is_file():
            dispatch_manifest = read_json(dispatch_manifest_path)
            if not isinstance(dispatch_manifest, dict):
                errors.append("dispatch_manifest.json 顶层必须是 JSON object")
            else:
                source_by_artifact = {
                    str(item.get("artifact_type") or ""): Path(str(item.get("path") or ""))
                    for item in artifact_sources
                    if isinstance(item, dict)
                }
                for artifact_type in expected_types:
                    source_path = source_by_artifact.get(artifact_type)
                    if source_path is None or not source_path.is_file():
                        continue
                    source_payload = read_json(source_path)
                    mapping, mapping_errors = artifact_mapping(source_payload)
                    errors.extend(str(error) for error in mapping_errors)
                    if artifact_type not in mapping:
                        continue
                    errors.extend(
                        validate_dispatch_identity(
                            source_payload,
                            bundle,
                            dispatch_manifest,
                            through_phase="pre_draft",
                            phase_order=PHASE_ORDER,
                        )
                    )
        else:
            errors.append("缺少 dispatch_manifest.json，无法校验 shard dispatch identity")

        # Existing main outputs are deliberately excluded from the pre-write
        # context check: --replace explicitly authorizes replacing them.
        context_artifacts = {
            key: value for key, value in artifacts.items() if key not in MAIN_OUTPUT_TYPES
        }
        errors.extend(artifact_context_errors(context_artifacts, bundle, task_dir))
        if errors:
            raise ValueError("; ".join(dict.fromkeys(errors)))

        results_by_id: dict[str, dict[str, Any]] = {}
        for artifact_type in expected_types:
            artifact = shard_artifacts[artifact_type]
            results = artifact.get("results") if isinstance(artifact, dict) else None
            if not isinstance(results, list):
                raise ValueError(f"{artifact_type}.results 必须是 JSON array")
            expected_ids = [
                _text(value) for value in shard_specs[artifact_type].get("candidate_ids", [])
            ]
            actual_ids: list[str] = []
            for index, result in enumerate(results, start=1):
                if not isinstance(result, dict):
                    raise ValueError(f"{artifact_type}.results[{index}] 必须是 JSON object")
                candidate_id = _text(result.get("candidate_id"))
                if candidate_id in results_by_id or candidate_id in actual_ids:
                    raise ValueError(f"entity verification candidate_id 重复: {candidate_id}")
                actual_ids.append(candidate_id)
                results_by_id[candidate_id] = result
            if actual_ids != expected_ids:
                raise ValueError(f"{artifact_type}.results 必须按 manifest 顺序恰好覆盖 candidate_ids")
        if len(results_by_id) != len(candidate_ids) or set(results_by_id) != set(candidate_ids):
            raise ValueError("entity verification shards 未按 manifest 恰好覆盖全部 candidates")

        forced_reasons, conflicts = _component_conflicts(
            candidates,
            candidate_ids,
            candidate_to_group,
            group_to_candidates,
            results_by_id,
        )
        candidate_by_id = {
            _text(candidate.get("candidate_id")): candidate for candidate in candidates
        }
        group_by_candidate = candidate_to_group
        components_by_candidate: dict[str, list[dict[str, Any]]] = {}
        # Reconstruct the same connected components for doubtful candidate
        # options; conflict decisions themselves are made above.
        uf = _UnionFind(candidate_ids)
        for members in group_to_candidates.values():
            for member in members[1:]:
                uf.union(members[0], member)
        by_identity: dict[str, list[str]] = {}
        for candidate_id in candidate_ids:
            identity_key = _text(results_by_id[candidate_id].get("identity_key"))
            if identity_key:
                by_identity.setdefault(identity_key, []).append(candidate_id)
        for members in by_identity.values():
            for member in members[1:]:
                uf.union(members[0], member)
        for candidate_id in candidate_ids:
            component_ids = [item for item in candidate_ids if uf.find(item) == uf.find(candidate_id)]
            components_by_candidate[candidate_id] = [results_by_id[item] for item in component_ids]

        items: list[str] = []
        confirmed_items: list[str] = []
        unresolved_items: list[str] = []
        external_evidence_paths: list[str] = []
        confirmed_item_evidence_paths: dict[str, list[str]] = {}
        doubtful_items: list[dict[str, Any]] = []
        for candidate_id in candidate_ids:
            candidate = candidate_by_id[candidate_id]
            result = results_by_id[candidate_id]
            term = _candidate_term(candidate, result)
            items.append(term)
            evidence_paths = _stable_unique(
                [_text(value) for value in result.get("evidence_paths", []) if _text(value)]
            )
            external_evidence_paths = _stable_unique(external_evidence_paths + evidence_paths)
            forced = forced_reasons.get(candidate_id, [])
            status = _text(result.get("status"))
            if status == "confirmed" and not forced:
                confirmed_items.append(term)
                confirmed_item_evidence_paths[term] = evidence_paths
            else:
                unresolved_items.append(term)
                doubtful_items.append(
                    _doubtful_item(
                        candidate,
                        result,
                        group_by_candidate[candidate_id],
                        forced,
                        components_by_candidate[candidate_id],
                    )
                )

        report = {
            "items": items,
            "local_candidate_paths": [],
            "external_evidence_paths": external_evidence_paths,
            "confirmed_item_evidence_paths": confirmed_item_evidence_paths,
            "confirmed_items": confirmed_items,
            "unresolved_items": unresolved_items,
            "conflicts": conflicts,
        }
        receipt = {
            "manifest_sha256": manifest_sha256,
            "shard_artifact_digest": canonical_json_digest(
                {artifact_type: shard_artifacts[artifact_type] for artifact_type in sorted(shard_artifacts)}
            ),
            "entity_report_sha256": canonical_json_digest(report),
            "doubtful_items_sha256": canonical_json_digest(doubtful_items),
            "candidate_ids": candidate_ids,
            "status": "assembled",
        }
        assembled_artifacts = {
            "entity_verification_report": report,
            "doubtful_items": doubtful_items,
            "entity_verification_assembly_receipt": receipt,
        }
        combined_artifacts = dict(context_artifacts)
        combined_artifacts.update(shard_artifacts)
        combined_artifacts.update(assembled_artifacts)
        final_validation = validate_payload({"artifacts": combined_artifacts})
        if final_validation.get("errors"):
            raise ValueError("; ".join(str(error) for error in final_validation["errors"]))
        context_errors = artifact_context_errors(combined_artifacts, bundle, task_dir)
        if context_errors:
            raise ValueError("; ".join(str(error) for error in context_errors))

        existing = [path for path in output_paths.values() if path.exists()]
        if existing and not replace:
            raise ValueError(
                "entity verification assembly artifact 已存在；显式传入 --replace 后才能替换: "
                + ", ".join(str(path) for path in existing)
            )
        envelopes = {
            artifact_type: _envelope(run_id, artifact_type, assembled_artifacts[artifact_type])
            for artifact_type in MAIN_OUTPUT_TYPES
        }
        for artifact_type in MAIN_OUTPUT_TYPES:
            _write_json(output_paths[artifact_type], envelopes[artifact_type])
        return {
            "ok": True,
            "mode": "parallel",
            "run_id": run_id,
            "manifest_sha256": manifest_sha256,
            "candidate_count": len(candidate_ids),
            "shard_count": len(expected_types),
            "confirmed_count": len(confirmed_items),
            "unresolved_count": len(unresolved_items),
            "conflict_count": len(conflicts),
            "artifacts": {artifact_type: str(output_paths[artifact_type]) for artifact_type in MAIN_OUTPUT_TYPES},
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="按 manifest 顺序组装并行实体核验 shard")
    parser.add_argument("task_dir", type=Path, help="MAS dispatch 目录")
    parser.add_argument("--replace", action="store_true", help="显式替换已有三项 main-owned artifact")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    try:
        result = assemble_entity_verification_shards(args.task_dir, replace=args.replace)
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
