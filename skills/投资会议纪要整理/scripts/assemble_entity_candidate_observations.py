#!/usr/bin/env python3
"""Validate and deterministically merge entity-discovery shard returns.

The worker boundary is intentionally strict: a return is exactly
``{"task_id": ..., "candidates": [...]}``, with no envelope, prose, network
evidence, or historical result.  This script is the only place where shard
observations become a candidate input for ``build_entity_candidate_manifest``.
It uses exact NFKC/casefold overlap only; fuzzy, phonetic, or completion-order
matching is never performed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
ARTIFACT_TYPE = "entity_discovery_assembly_receipt"
MANIFEST_ARTIFACT_TYPE = "entity_candidate_discovery_input"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_CANDIDATE_FIELDS = {
    "candidate_term",
    "observed_forms",
    "verification_kinds",
    "risk_level",
    "verification_reason_codes",
    "source_turn_ids",
}
ALLOWED_RETURN_FIELDS = {"task_id", "candidates"}
ALLOWED_VERIFICATION_KINDS = {"company_identity", "security_code", "industry_term"}
ALLOWED_REASON_CODES = {
    "source_identity_unclear",
    "source_conflict",
    "abbreviation_ambiguous",
    "local_multiple_candidates",
    "local_not_found",
    "confirmed_code_required",
}
RISK_ORDER = {"low": 1, "medium": 2, "high": 3}


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _display(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 string")
    return unicodedata.normalize("NFKC", value).strip()


def _literal(value: Any, field: str) -> str:
    """Preserve the observed source form while trimming framing whitespace."""

    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 string")
    return value.strip()


def _identity(value: str) -> str:
    # This is deliberately exact.  Do not add punctuation stripping, token
    # extraction, pinyin, edit distance, or other fuzzy matching here.
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _sha256(value: Any, field: str) -> str:
    result = _display(value, field)
    if not SHA256_RE.fullmatch(result):
        raise ValueError(f"{field} 必须是 64 位小写 SHA-256")
    return result


def _read_json(path: Path) -> Any:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"{path} 必须是 UTF-8 without BOM")
    return json.loads(raw.decode("utf-8"))


def _write_bytes_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def write_json_atomic(path: Path, payload: Any) -> None:
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_bytes_atomic(path, raw)


def _validate_plan(plan: Any) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("entity_discovery_plan 顶层必须是 JSON object")
    if plan.get("artifact_type") not in (None, "entity_discovery_plan"):
        raise ValueError("输入不是 entity_discovery_plan")
    source_sha256 = _sha256(plan.get("source_sha256"), "plan.source_sha256")
    raw_tasks = plan.get("tasks")
    if raw_tasks is None:
        raw_tasks = plan.get("shards")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("plan.tasks/shards 必须是非空 JSON array")
    if plan.get("tasks") is not None and plan.get("shards") is not None:
        if plan.get("tasks") != plan.get("shards"):
            raise ValueError("plan.tasks 与 plan.shards 不一致")
    raw_turn_ids = plan.get("turn_ids")
    if not isinstance(raw_turn_ids, list) or not raw_turn_ids:
        raise ValueError("plan.turn_ids 必须是非空 JSON array")
    turn_ids = [_display(item, "plan.turn_ids") for item in raw_turn_ids]
    if len(turn_ids) != len(set(turn_ids)):
        raise ValueError("plan.turn_ids 不得重复")

    # Validate the plan digest if present.  The digest is optional for a
    # programmatic caller, but a malformed declared digest must never pass.
    declared_plan_hash = plan.get("plan_sha256")
    if declared_plan_hash not in (None, ""):
        declared_plan_hash = _sha256(declared_plan_hash, "plan.plan_sha256")
        unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
        if declared_plan_hash != canonical_sha256(unsigned):
            raise ValueError("entity_discovery_plan.plan_sha256 与内容不一致")

    tasks: list[dict[str, Any]] = []
    task_ids: set[str] = set()
    assigned_turns: list[str] = []
    sequence_by_turn: dict[str, int] = {}
    # ``turn_sequences`` is the trusted global ordering emitted by the plan.
    # We do not infer source order from return completion order.
    for index, raw in enumerate(raw_tasks, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"plan.tasks[{index}] 必须是 JSON object")
        task_id = _display(raw.get("task_id"), f"plan.tasks[{index}].task_id")
        if not task_id or task_id in task_ids:
            raise ValueError("plan.tasks.task_id 必须唯一且非空")
        task_turn_ids = raw.get("turn_ids")
        if not isinstance(task_turn_ids, list) or not task_turn_ids:
            raise ValueError(f"{task_id}.turn_ids 必须是非空 JSON array")
        task_turn_ids = [_display(item, f"{task_id}.turn_ids") for item in task_turn_ids]
        if len(task_turn_ids) != len(set(task_turn_ids)):
            raise ValueError(f"{task_id}.turn_ids 不得重复")
        if any(turn_id not in turn_ids for turn_id in task_turn_ids):
            raise ValueError(f"{task_id}.turn_ids 含未知 turn_id")
        turn_sequences = raw.get("turn_sequences")
        if not isinstance(turn_sequences, list) or len(turn_sequences) != len(task_turn_ids):
            raise ValueError(f"{task_id}.turn_sequences 必须与 turn_ids 一一对应")
        checked_sequences: list[int] = []
        for seq_index, sequence in enumerate(turn_sequences):
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
                raise ValueError(f"{task_id}.turn_sequences[{seq_index}] 必须是正整数")
            checked_sequences.append(sequence)
            previous = sequence_by_turn.get(task_turn_ids[seq_index])
            if previous is not None and previous != sequence:
                raise ValueError(f"turn_id {task_turn_ids[seq_index]} 绑定了多个 sequence")
            sequence_by_turn[task_turn_ids[seq_index]] = sequence
        if checked_sequences != sorted(checked_sequences):
            raise ValueError(f"{task_id}.turn_sequences 必须按 source sequence 排列")
        if checked_sequences != list(
            range(checked_sequences[0], checked_sequences[-1] + 1)
        ):
            raise ValueError(f"{task_id} 必须覆盖连续 source turns")
        tasks.append(
            {
                "task_id": task_id,
                "turn_ids": task_turn_ids,
                "turn_sequences": checked_sequences,
            }
        )
        task_ids.add(task_id)
        assigned_turns.extend(task_turn_ids)
    if len(assigned_turns) != len(set(assigned_turns)) or set(assigned_turns) != set(turn_ids):
        raise ValueError("plan.tasks 必须恰好覆盖每个 turn 一次")
    declared_turn_count = plan.get("turn_count")
    if declared_turn_count not in (None, "") and declared_turn_count != len(turn_ids):
        raise ValueError("plan.turn_count 与 turn_ids 不一致")
    declared_shard_count = plan.get("shard_count")
    if declared_shard_count not in (None, "") and declared_shard_count != len(tasks):
        raise ValueError("plan.shard_count 与 tasks 不一致")
    waves = plan.get("dispatch_waves")
    if waves is not None:
        if not isinstance(waves, list) or not waves:
            raise ValueError("plan.dispatch_waves 必须是非空 JSON array")
        wave_task_ids: list[str] = []
        for wave_index, raw_wave in enumerate(waves, start=1):
            if not isinstance(raw_wave, dict):
                raise ValueError(f"plan.dispatch_waves[{wave_index}] 必须是 JSON object")
            raw_wave_ids = raw_wave.get("task_ids")
            if not isinstance(raw_wave_ids, list) or not raw_wave_ids:
                raise ValueError(f"plan.dispatch_waves[{wave_index}].task_ids 不得为空")
            wave_task_ids.extend(_display(item, f"plan.dispatch_waves[{wave_index}].task_ids") for item in raw_wave_ids)
        if len(wave_task_ids) != len(set(wave_task_ids)) or set(wave_task_ids) != set(task_ids):
            raise ValueError("plan.dispatch_waves 必须恰好覆盖每个 task 一次")
        if len(waves) != 1:
            raise ValueError("entity discovery 必须从第一波并行派发全部 shards")
    ordered_turns = sorted(turn_ids, key=lambda turn_id: (sequence_by_turn.get(turn_id, 10**18), turn_id))
    if list(turn_ids) != ordered_turns:
        # A plan from the builder is source ordered.  Rejecting a reordered
        # plan protects the representative-selection tie break.
        raise ValueError("plan.turn_ids 必须按 source sequence 排列")
    if sorted(sequence_by_turn.values()) != list(range(1, len(turn_ids) + 1)):
        raise ValueError("plan turn sequences 必须从 1 连续编号")
    return {
        "source_sha256": source_sha256,
        "plan_sha256": declared_plan_hash or canonical_sha256(plan),
        "turn_ids": turn_ids,
        "sequence_by_turn": sequence_by_turn,
        "tasks": tasks,
        "task_by_id": {task["task_id"]: task for task in tasks},
    }


def _string_list(value: Any, field: str, *, nonempty: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是 JSON array")
    result = [_display(item, field) for item in value]
    if any(not item for item in result):
        raise ValueError(f"{field} 不得包含空字符串")
    if len(result) != len(set(result)):
        raise ValueError(f"{field} 不得重复")
    if nonempty and not result:
        raise ValueError(f"{field} 不得为空")
    return result


def _literal_list(value: Any, field: str, *, nonempty: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是 JSON array")
    result = [_literal(item, field) for item in value]
    if any(not item for item in result):
        raise ValueError(f"{field} 不得包含空字符串")
    if len(result) != len(set(result)):
        raise ValueError(f"{field} 不得重复")
    if nonempty and not result:
        raise ValueError(f"{field} 不得为空")
    return result


def _validate_candidate(raw: Any, task: dict[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{task['task_id']}.candidates[{index}] 必须是 JSON object")
    unknown = sorted(set(raw) - ALLOWED_CANDIDATE_FIELDS)
    missing = sorted(ALLOWED_CANDIDATE_FIELDS - set(raw))
    if unknown:
        raise ValueError(f"{task['task_id']}.candidates[{index}] 含未知字段: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{task['task_id']}.candidates[{index}] 缺少字段: {', '.join(missing)}")
    term = _literal(raw.get("candidate_term"), f"{task['task_id']}.candidates[{index}].candidate_term")
    if not term:
        raise ValueError(f"{task['task_id']}.candidates[{index}].candidate_term 不得为空")
    if len(term) > 160:
        raise ValueError(f"{task['task_id']}.candidates[{index}].candidate_term 不得超过 160 字符")
    forms = _literal_list(
        raw.get("observed_forms"),
        f"{task['task_id']}.candidates[{index}].observed_forms",
        nonempty=False,
    )
    kinds = _string_list(
        raw.get("verification_kinds"),
        f"{task['task_id']}.candidates[{index}].verification_kinds",
    )
    unknown_kinds = sorted(set(kinds) - ALLOWED_VERIFICATION_KINDS)
    if unknown_kinds:
        raise ValueError("不支持的 verification_kinds: " + ", ".join(unknown_kinds))
    risk = _display(raw.get("risk_level"), f"{task['task_id']}.candidates[{index}].risk_level").casefold()
    if risk not in RISK_ORDER:
        raise ValueError("risk_level 必须是 low、medium 或 high")
    reasons = _string_list(
        raw.get("verification_reason_codes"),
        f"{task['task_id']}.candidates[{index}].verification_reason_codes",
    )
    unknown_reasons = sorted(set(reasons) - ALLOWED_REASON_CODES)
    if unknown_reasons:
        raise ValueError("不支持的 verification_reason_codes: " + ", ".join(unknown_reasons))
    source_turn_ids = _string_list(
        raw.get("source_turn_ids"),
        f"{task['task_id']}.candidates[{index}].source_turn_ids",
    )
    assigned = set(task["turn_ids"])
    if any(turn_id not in assigned for turn_id in source_turn_ids):
        raise ValueError(
            f"{task['task_id']}.candidates[{index}].source_turn_ids 必须属于该 shard"
        )
    return {
        "candidate_term": term,
        "observed_forms": forms,
        "verification_kinds": sorted(set(kinds)),
        "risk_level": risk,
        "verification_reason_codes": sorted(set(reasons)),
        "source_turn_ids": source_turn_ids,
    }


def _validate_returns(plan: dict[str, Any], returns: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    by_task: dict[str, list[dict[str, Any]]] = {}
    expected = set(plan["task_by_id"])
    for return_index, raw in enumerate(returns, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"return[{return_index}] 顶层必须是 JSON object")
        unknown = sorted(set(raw) - ALLOWED_RETURN_FIELDS)
        missing = sorted(ALLOWED_RETURN_FIELDS - set(raw))
        if unknown:
            raise ValueError(f"return[{return_index}] 含未知字段: {', '.join(unknown)}")
        if missing:
            raise ValueError(f"return[{return_index}] 缺少字段: {', '.join(missing)}")
        task_id = _display(raw.get("task_id"), f"return[{return_index}].task_id")
        if not task_id:
            raise ValueError(f"return[{return_index}].task_id 不得为空")
        if task_id not in expected:
            raise ValueError(f"return[{return_index}] 含未知 task_id: {task_id}")
        if task_id in by_task:
            raise ValueError(f"task_id 重复返回: {task_id}")
        candidates = raw.get("candidates")
        if not isinstance(candidates, list):
            raise ValueError(f"{task_id}.candidates 必须是 JSON array")
        task = plan["task_by_id"][task_id]
        by_task[task_id] = [
            _validate_candidate(item, task, index) for index, item in enumerate(candidates, start=1)
        ]
    missing_tasks = sorted(expected - set(by_task))
    if missing_tasks:
        raise ValueError("缺少 discovery task 返回: " + ", ".join(missing_tasks))
    return by_task


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def _candidate_sort_key(candidate: dict[str, Any], sequence_by_turn: dict[str, int]) -> tuple[Any, ...]:
    source_sequences = sorted(sequence_by_turn[turn_id] for turn_id in candidate["source_turn_ids"])
    earliest = source_sequences[0] if source_sequences else 10**18
    normalized_term = _identity(candidate["candidate_term"])
    return (
        earliest,
        normalized_term,
        candidate["candidate_term"],
        tuple(sorted(_identity(value) for value in candidate["observed_forms"])),
        tuple(candidate["verification_kinds"]),
        candidate["risk_level"],
        tuple(candidate["verification_reason_codes"]),
        tuple(sorted(candidate["source_turn_ids"])),
        tuple(source_sequences),
    )


def _stable_candidate_id(term: str, kinds: list[str]) -> str:
    # Match build_entity_candidate_manifest.py's identity choice: adding a
    # newly observed alias or changing discovery order must not rename a term.
    identity_key = canonical_sha256(
        {"term": _identity(term), "verification_kinds": sorted(kinds)}
    )
    # Keep the same 12-hex suffix convention used by
    # build_entity_candidate_manifest.py for generated candidate IDs.
    return "candidate_" + hashlib.sha256(identity_key.encode("ascii")).hexdigest()[:12]


def _merge_candidates(
    plan: dict[str, Any], by_task: dict[str, list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    # Iterate in trusted plan task order, not in return completion order.
    for task in plan["tasks"]:
        task_id = task["task_id"]
        for ordinal, candidate in enumerate(by_task[task_id], start=1):
            observations.append(
                {
                    **candidate,
                    "task_id": task_id,
                    "ordinal": ordinal,
                }
            )
    union_find = _UnionFind(len(observations))
    value_owner: dict[str, int] = {}
    for index, candidate in enumerate(observations):
        values = {
            _identity(candidate["candidate_term"]),
            *(_identity(value) for value in candidate["observed_forms"]),
        }
        values.discard("")
        for value in sorted(values):
            previous = value_owner.get(value)
            if previous is None:
                value_owner[value] = index
            else:
                union_find.union(previous, index)

    components: dict[int, list[dict[str, Any]]] = {}
    for index, observation in enumerate(observations):
        components.setdefault(union_find.find(index), []).append(observation)
    ordered_components = sorted(
        components.values(),
        key=lambda values: min(
            _candidate_sort_key(value, plan["sequence_by_turn"]) for value in values
        ),
    )

    merged: list[dict[str, Any]] = []
    merged_source_turn_ids: list[dict[str, Any]] = []
    for members in ordered_components:
        representative = min(
            members,
            key=lambda value: _candidate_sort_key(value, plan["sequence_by_turn"]),
        )
        forms: dict[str, str] = {}
        source_turn_ids: set[str] = set()
        kinds: set[str] = set()
        reasons: set[str] = set()
        risk = "low"
        for member in members:
            for form in member["observed_forms"]:
                key = _identity(form)
                if key and (key not in forms or (form, key) < (forms[key], key)):
                    forms[key] = form
            source_turn_ids.update(member["source_turn_ids"])
            kinds.update(member["verification_kinds"])
            reasons.update(member["verification_reason_codes"])
            if RISK_ORDER[member["risk_level"]] > RISK_ORDER[risk]:
                risk = member["risk_level"]
        ordered_source_turn_ids = sorted(
            source_turn_ids,
            key=lambda turn_id: (plan["sequence_by_turn"][turn_id], turn_id),
        )
        ordered_forms = [forms[key] for key in sorted(forms)]
        ordered_kinds = sorted(kinds)
        ordered_reasons = sorted(reasons)
        candidate_id = _stable_candidate_id(representative["candidate_term"], ordered_kinds)
        merged.append(
            {
                "candidate_id": candidate_id,
                "candidate_term": representative["candidate_term"],
                # ``aliases`` is the vocabulary consumed by
                # build_entity_candidate_manifest.py.  Keep the private
                # source-turn provenance in the receipt, not in the public
                # network-verification packet.
                "aliases": ordered_forms,
                "verification_kinds": ordered_kinds,
                "risk_level": risk,
                "verification_reason_codes": ordered_reasons,
                "network_verification_required": True,
            }
        )
        merged_source_turn_ids.append(
            {"candidate_id": candidate_id, "source_turn_ids": ordered_source_turn_ids}
        )
    return merged, {
        "observation_count": len(observations),
        "component_count": len(merged),
        "component_source_turn_ids": [
            # This field is process metadata and is useful when reviewing why
            # two exact-overlap observations were merged.
            sorted(
                {
                    turn_id
                    for member in members
                    for turn_id in member["source_turn_ids"]
                },
                key=lambda turn_id: (plan["sequence_by_turn"][turn_id], turn_id),
            )
            for members in ordered_components
        ],
        "candidate_source_turn_ids": merged_source_turn_ids,
    }


def assemble_observations(plan: Any, returns: Iterable[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(build_entity_candidate_manifest_input, assembly_receipt)``."""

    if isinstance(plan, (str, Path)):
        plan = _read_json(Path(plan).expanduser())
    returns = list(returns)
    if returns and all(isinstance(value, (str, Path)) for value in returns):
        returns = [_read_json(Path(value).expanduser()) for value in returns]
    checked_plan = _validate_plan(plan)
    by_task = _validate_returns(checked_plan, returns)
    candidates, merge_stats = _merge_candidates(checked_plan, by_task)
    candidate_manifest = {
        "source_sha256": checked_plan["source_sha256"],
        "candidates": candidates,
    }
    candidate_manifest_sha256 = canonical_sha256(candidate_manifest)
    return_digest_payload = {
        task_id: sorted(
            by_task[task_id],
            key=lambda candidate: _candidate_sort_key(
                candidate, checked_plan["sequence_by_turn"]
            ),
        )
        for task_id in sorted(by_task)
    }
    return_digest = canonical_sha256(return_digest_payload)
    expected_turn_ids = list(checked_plan["turn_ids"])
    # Every assigned turn is covered by exactly one task.  Candidate source
    # turn ids are intentionally a separate, possibly sparse, observation map:
    # an empty shard return is valid and still covers its assigned turns.
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "status": "assembled",
        "source_sha256": checked_plan["source_sha256"],
        "plan_sha256": checked_plan["plan_sha256"],
        "return_digest": return_digest,
        "candidate_manifest_sha256": candidate_manifest_sha256,
        "task_ids": [task["task_id"] for task in checked_plan["tasks"]],
        "task_count": len(checked_plan["tasks"]),
        "candidate_observation_count": merge_stats["observation_count"],
        "candidate_count": len(candidates),
        "turn_coverage": {
            "turn_count": len(expected_turn_ids),
            "expected_turn_ids": expected_turn_ids,
            "assigned_turn_ids": expected_turn_ids,
            "returned_task_turn_ids": expected_turn_ids,
            "covered_turn_ids": expected_turn_ids,
            "missing_turn_ids": [],
            "unexpected_turn_ids": [],
            "complete": True,
        },
        "component_count": merge_stats["component_count"],
        "component_source_turn_ids": merge_stats["component_source_turn_ids"],
        "candidate_source_turn_ids": merge_stats["candidate_source_turn_ids"],
    }
    return candidate_manifest, receipt


# Stable descriptive alias for callers that use the script name as the
# function name.
assemble_entity_candidate_observations = assemble_observations


def _return_paths(paths: Iterable[Path], returns_dir: Path | None) -> list[Path]:
    resolved: list[Path] = []
    for path in paths:
        path = path.expanduser()
        if path.is_dir():
            # Directory input is explicit and deterministic; no recursive or
            # broad workspace scan is performed.
            resolved.extend(sorted(path.glob("*.json"), key=lambda item: item.name))
        else:
            resolved.append(path)
    if returns_dir is not None:
        directory = returns_dir.expanduser()
        if not directory.is_dir():
            raise ValueError(f"returns-dir 不是目录: {directory}")
        resolved.extend(sorted(directory.glob("*.json"), key=lambda item: item.name))
    if not resolved:
        raise ValueError("必须提供至少一个 compact return JSON（路径或目录）")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in resolved:
        absolute = path.resolve()
        if absolute in seen:
            raise ValueError(f"compact return 路径重复: {path}")
        seen.add(absolute)
        unique.append(path)
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="fail-closed 校验并确定性合并 entity discovery compact returns"
    )
    parser.add_argument("plan_positional", nargs="?", type=Path, help="entity_discovery_plan JSON")
    parser.add_argument("--plan", dest="plan_option", type=Path, help="entity_discovery_plan JSON")
    parser.add_argument("returns", nargs="*", type=Path, help="compact return JSON 路径或目录")
    parser.add_argument(
        "--return-json",
        dest="return_options",
        action="append",
        type=Path,
        help="compact return JSON 路径（可重复）",
    )
    parser.add_argument("--returns-dir", type=Path, help="compact return JSON 所在目录")
    parser.add_argument(
        "--out-manifest",
        "--out",
        dest="out_manifest",
        type=Path,
        required=True,
        help="build_entity_candidate_manifest.py 可消费的 JSON 输出路径",
    )
    parser.add_argument(
        "--out-receipt",
        type=Path,
        required=True,
        help="entity discovery assembly receipt 输出路径",
    )
    args = parser.parse_args(argv)
    try:
        plan_path = args.plan_option or args.plan_positional
        if plan_path is None:
            raise ValueError("必须提供 entity_discovery_plan JSON 路径")
        plan = _read_json(plan_path.expanduser())
        return_paths = list(args.returns)
        return_paths.extend(args.return_options or [])
        return_payloads = [_read_json(path) for path in _return_paths(return_paths, args.returns_dir)]
        candidate_manifest, receipt = assemble_observations(plan, return_payloads)
        # All validation and merging has completed before either destination is
        # touched.  Each destination is then replaced atomically.
        write_json_atomic(args.out_manifest.expanduser(), candidate_manifest)
        write_json_atomic(args.out_receipt.expanduser(), receipt)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "out_manifest": str(args.out_manifest),
                "out_receipt": str(args.out_receipt),
                "candidate_count": len(candidate_manifest["candidates"]),
                "task_count": receipt["task_count"],
                "source_sha256": receipt["source_sha256"],
                "candidate_manifest_sha256": receipt["candidate_manifest_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
