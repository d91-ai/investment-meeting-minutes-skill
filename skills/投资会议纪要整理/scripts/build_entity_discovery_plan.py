#!/usr/bin/env python3
"""Build a source-bound, parallel entity-candidate discovery plan.

This step is deliberately smaller than ``build_entity_candidate_manifest.py``.
It does not try to recognise names with regular expressions and it never calls
the network.  Instead it reuses the already source-ordered, capacity-bounded
shards emitted by ``build_speaker_turn_manifest.py`` and gives each shard to a
discovery worker.  The worker sees only its private turns and is required to
return a tiny JSON object; the deterministic merge is implemented by the
companion ``assemble_entity_candidate_observations.py`` script.
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
ARTIFACT_TYPE = "entity_discovery_plan"
TASK_PREFIX = "entity_discovery__shard_"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(value: Any) -> str:
    """Hash canonical UTF-8 JSON, matching the repository's other builders."""

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
    # Keep source text private and source-faithful.  NFKC makes the digest
    # stable for full-width punctuation while strip removes framing spaces.
    result = unicodedata.normalize("NFKC", value).strip()
    return result


def _source_string(value: Any, field: str, *, allow_empty: bool = True) -> str:
    """Validate source metadata without normalising private source text."""

    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 string")
    if not allow_empty and not value:
        raise ValueError(f"{field} 不得为空")
    return value


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
    """Write a complete artifact without exposing a partial JSON/prompt file."""

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


def _manifest_hash(manifest: dict[str, Any]) -> str:
    """Return the hash emitted by build_speaker_turn_manifest.py.

    That builder computes ``manifest_sha256`` before adding the field itself.
    Recomputing the same pre-self-reference payload both verifies a declared
    hash and provides a useful binding when an older manifest omitted it.
    """

    unsigned = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    calculated = canonical_sha256(unsigned)
    declared = manifest.get("manifest_sha256")
    if declared not in (None, ""):
        declared_hash = _sha256(declared, "manifest_sha256")
        if declared_hash != calculated:
            raise ValueError("speaker_turn_manifest.manifest_sha256 与内容不一致")
        return declared_hash
    return calculated


def _turns_and_shards(manifest: dict[str, Any]) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    if manifest.get("artifact_type") not in (None, "speaker_turn_manifest"):
        raise ValueError("输入必须是 build_speaker_turn_manifest.py 生成的 speaker_turn_manifest")
    source_sha256 = _sha256(manifest.get("source_sha256"), "source_sha256")
    raw_turns = manifest.get("turns")
    raw_shards = manifest.get("shards")
    if not isinstance(raw_turns, list) or not raw_turns:
        raise ValueError("speaker_turn_manifest.turns 必须是非空 JSON array")
    if not isinstance(raw_shards, list) or not raw_shards:
        raise ValueError("speaker_turn_manifest.shards 必须是非空 JSON array")

    turns: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    sequences: set[int] = set()
    for index, raw in enumerate(raw_turns, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"turns[{index}] 必须是 JSON object")
        turn_id = _display(raw.get("turn_id"), f"turns[{index}].turn_id")
        if not turn_id or turn_id in by_id:
            raise ValueError("speaker_turn_manifest.turn_id 必须唯一且非空")
        sequence = raw.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise ValueError(f"turns[{index}].sequence 必须是正整数")
        if sequence in sequences:
            raise ValueError("speaker_turn_manifest.sequence 必须唯一")
        # The source turn is private input, not an identity key.  Preserve it
        # byte-for-byte (as decoded UTF-8) so a prompt cannot silently change
        # a full-width form, punctuation, or a speaker's trailing qualifier.
        text = _source_string(raw.get("text"), f"turns[{index}].text", allow_empty=False)
        if not text:
            raise ValueError(f"turns[{index}].text 不得为空")
        # Do not copy arbitrary source metadata into the prompt.  The three
        # fields below are sufficient for discovery and for assembly coverage.
        turn = {
            "turn_id": turn_id,
            "sequence": sequence,
            "speaker_label": _source_string(
                raw.get("speaker_label", ""), f"turns[{index}].speaker_label"
            )
            if raw.get("speaker_label") not in (None, "")
            else "",
            "text": text,
        }
        turns.append(turn)
        by_id[turn_id] = turn
        sequences.add(sequence)
    turns.sort(key=lambda item: (int(item["sequence"]), str(item["turn_id"])))
    expected_sequences = list(range(1, len(turns) + 1))
    if [int(turn["sequence"]) for turn in turns] != expected_sequences:
        raise ValueError("speaker_turn_manifest.sequence 必须从 1 连续编号")
    declared_turn_count = manifest.get("turn_count")
    if declared_turn_count not in (None, "") and declared_turn_count != len(turns):
        raise ValueError("speaker_turn_manifest.turn_count 与 turns 不一致")

    shards: list[dict[str, Any]] = []
    assigned: list[str] = []
    shard_ids: set[str] = set()
    for index, raw in enumerate(raw_shards, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"shards[{index}] 必须是 JSON object")
        source_shard_id = _display(
            raw.get("shard_id") or raw.get("artifact_type"),
            f"shards[{index}].shard_id",
        )
        if not source_shard_id or source_shard_id in shard_ids:
            raise ValueError("speaker_turn_manifest.shard_id 必须唯一且非空")
        raw_turn_ids = raw.get("turn_ids")
        if not isinstance(raw_turn_ids, list) or not raw_turn_ids:
            raise ValueError(f"shards[{index}].turn_ids 必须是非空 JSON array")
        turn_ids = [_display(value, f"shards[{index}].turn_ids") for value in raw_turn_ids]
        if len(turn_ids) != len(set(turn_ids)):
            raise ValueError(f"{source_shard_id}.turn_ids 不得重复")
        if any(turn_id not in by_id for turn_id in turn_ids):
            raise ValueError(f"{source_shard_id}.turn_ids 含未知 turn_id")
        shard_turns = [by_id[turn_id] for turn_id in turn_ids]
        sequences_in_shard = [int(turn["sequence"]) for turn in shard_turns]
        if sequences_in_shard != sorted(sequences_in_shard):
            raise ValueError(f"{source_shard_id}.turn_ids 必须按 source sequence 排列")
        if sequences_in_shard != list(
            range(sequences_in_shard[0], sequences_in_shard[-1] + 1)
        ):
            raise ValueError(f"{source_shard_id} 必须复用连续 speaker turns")
        shards.append(
            {
                "source_shard_id": source_shard_id,
                "turn_ids": turn_ids,
                "turn_sequences": sequences_in_shard,
            }
        )
        shard_ids.add(source_shard_id)
        assigned.extend(turn_ids)
    if len(assigned) != len(set(assigned)):
        raise ValueError("speaker_turn_manifest.shards 不得重叠")
    if set(assigned) != set(by_id):
        missing = sorted(set(by_id) - set(assigned), key=lambda value: int(by_id[value]["sequence"]))
        extra = sorted(set(assigned) - set(by_id))
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if extra:
            detail.append("extra=" + ",".join(extra))
        raise ValueError("speaker_turn_manifest.shards 未完整覆盖 turns: " + "; ".join(detail))
    # The source manifest creates shards in source order.  Enforce that here so
    # a hand-edited manifest cannot silently change the first-dispatch order.
    assigned_sequences = [int(by_id[turn_id]["sequence"]) for turn_id in assigned]
    if assigned_sequences != list(range(1, len(turns) + 1)):
        raise ValueError("speaker_turn_manifest.shards 必须按全局 source sequence 连续排列")
    declared_shard_count = manifest.get("shard_count")
    if declared_shard_count not in (None, "") and declared_shard_count != len(shards):
        raise ValueError("speaker_turn_manifest.shard_count 与 shards 不一致")
    return source_sha256, turns, shards


def _prompt(task_id: str, source_sha256: str, shard: dict[str, Any], turns: Iterable[dict[str, Any]]) -> str:
    private_turns = [
        {
            "turn_id": turn["turn_id"],
            "sequence": turn["sequence"],
            "speaker_label": turn["speaker_label"],
            "text": turn["text"],
        }
        for turn in turns
    ]
    # The prose intentionally says "no network" and "no historical result".
    # Those are safety constraints for the worker, not candidate facts.
    return (
        "你是当前会议的实体候选发现助手。只阅读下面分配给你的 source turns；"
        "不得联网、不得调用外部检索、不得读取或引用历史结果/缓存，也不得把客户关系、订单、"
        "数字、日期、预测或其他会议事实当作需要核验的实体。\n\n"
        f"task_id: {task_id}\n"
        f"source_sha256: {source_sha256}\n"
        f"assigned_turn_ids: {json.dumps(shard['turn_ids'], ensure_ascii=False)}\n\n"
        "只发现身份可能不确定的公司名称、证券代码或行业/产品术语。稳定且无歧义的提及不必输出。"
        "不要猜测、模糊匹配、拼音匹配或补写 source 中没有出现的词。"
        "公司名或术语仅仅在原文出现、你不熟悉它、或它可能需要股票代码，都不是候选理由。"
        "只有 source 本身给出可观察的身份不确定信号才输出：同一指代出现冲突写法、"
        "明显的转写/同音错误形态、无法从本片展开的缩写/代码，或原文明示不确定。"
        "不得因为没有进行本地数据库查询而声明 local_not_found/local_multiple_candidates；"
        "不得为稳定公司全量建立待核验清单。\n\n"
        "每个候选必须严格使用以下字段且不得增加字段：candidate_term、observed_forms、"
        "verification_kinds、risk_level、verification_reason_codes、source_turn_ids。"
        "verification_kinds 只能是 company_identity、security_code、industry_term；"
        "verification_reason_codes 只能是 source_identity_unclear、source_conflict、"
        "abbreviation_ambiguous、local_multiple_candidates、local_not_found、"
        "confirmed_code_required；risk_level 只能是 low、medium、high。"
        "source_turn_ids 只能来自 assigned_turn_ids 且不得为空。\n\n"
        "返回值必须是紧凑 JSON（不要 Markdown 代码围栏或解释文字），顶层只能有 task_id 和 candidates："
        '{"task_id":"...","candidates":[{"candidate_term":"...","observed_forms":["..."],'
        '"verification_kinds":["company_identity"],"risk_level":"medium",'
        '"verification_reason_codes":["source_identity_unclear"],"source_turn_ids":["turn_000001"]}]}\n\n'
        "assigned source turns（私有输入，仅用于本次判断）：\n"
        + json.dumps(private_turns, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    )


def build_plan(manifest: dict[str, Any] | Path | str, *, prompt_dir: Path | None = None) -> dict[str, Any]:
    """Validate a speaker-turn manifest and create independent shard prompts."""

    if isinstance(manifest, (str, Path)):
        manifest = _read_json(Path(manifest).expanduser())
    if not isinstance(manifest, dict):
        raise ValueError("speaker_turn_manifest 顶层必须是 JSON object")
    source_sha256, turns, shards = _turns_and_shards(manifest)
    manifest_sha256 = _manifest_hash(manifest)
    by_id = {str(turn["turn_id"]): turn for turn in turns}
    tasks: list[dict[str, Any]] = []
    prompt_payloads: list[tuple[Path, bytes]] = []
    prompt_root = prompt_dir.expanduser() if prompt_dir is not None else None
    for number, shard in enumerate(shards, start=1):
        task_id = f"{TASK_PREFIX}{number:03d}"
        shard_turns = [by_id[turn_id] for turn_id in shard["turn_ids"]]
        prompt = _prompt(task_id, source_sha256, shard, shard_turns)
        prompt_bytes = prompt.encode("utf-8")
        prompt_name = f"{task_id}.prompt.md"
        # Store only a relative filename.  An absolute local path would leak
        # workspace details into a reusable plan; the CLI tells dispatch code
        # which prompt directory was selected.
        prompt_file = prompt_name
        if prompt_root is not None:
            prompt_payloads.append((prompt_root / prompt_name, prompt_bytes))
        tasks.append(
            {
                "task_id": task_id,
                "shard_id": shard["source_shard_id"],
                "source_shard_id": shard["source_shard_id"],
                "source_sha256": source_sha256,
                "turn_ids": list(shard["turn_ids"]),
                "turn_sequences": list(shard["turn_sequences"]),
                "turn_count": len(shard["turn_ids"]),
                "prompt_file": prompt_file,
                "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
                # Keeping the prompt in the plan makes a plan self-contained
                # for review while the separate file remains convenient for
                # dispatch tooling.
                "prompt": prompt,
            }
        )

    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": ARTIFACT_TYPE,
        "source_sha256": source_sha256,
        "speaker_turn_manifest_sha256": manifest_sha256,
        "turn_count": len(turns),
        "shard_count": len(tasks),
        "parallel_from_start": True,
        "dispatch_waves": [
            {"wave": 1, "task_ids": [task["task_id"] for task in tasks]}
        ],
        "turn_ids": [turn["turn_id"] for turn in turns],
        "tasks": tasks,
        # ``shards`` mirrors the input manifest vocabulary; ``tasks`` is the
        # dispatch-oriented alias consumed by the assembler.
        "shards": tasks,
    }
    plan["plan_sha256"] = canonical_sha256(plan)
    if prompt_root is not None:
        for path, raw in prompt_payloads:
            _write_bytes_atomic(path, raw)
    return plan


# Descriptive aliases make the module convenient to call from orchestration
# code without changing the compact CLI contract.
build_entity_discovery_plan = build_plan


def _resolve_manifest_path(args: argparse.Namespace) -> Path:
    path = args.manifest_option or args.manifest_positional
    if path is None:
        raise ValueError("必须提供 speaker_turn_manifest JSON 路径")
    return path.expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从 speaker_turn_manifest 复用连续 turns，生成并行实体候选发现 plan 与 prompts"
    )
    parser.add_argument("manifest_positional", nargs="?", type=Path, help="speaker_turn_manifest JSON")
    parser.add_argument("--manifest", dest="manifest_option", type=Path, help="speaker_turn_manifest JSON")
    parser.add_argument("--out", required=True, type=Path, help="discovery plan JSON 输出路径")
    parser.add_argument(
        "--prompt-dir",
        type=Path,
        help="prompt 输出目录（默认与 plan 同目录的 <plan-stem>_prompts）",
    )
    args = parser.parse_args(argv)
    try:
        manifest_path = _resolve_manifest_path(args)
        manifest = _read_json(manifest_path)
        prompt_dir = args.prompt_dir
        if prompt_dir is None:
            prompt_dir = args.out.expanduser().parent / f"{args.out.stem}_prompts"
        plan = build_plan(manifest, prompt_dir=prompt_dir)
        write_json_atomic(args.out.expanduser(), plan)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "out": str(args.out),
                "task_count": len(plan["tasks"]),
                "turn_count": plan["turn_count"],
                "source_sha256": plan["source_sha256"],
                "plan_sha256": plan["plan_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
