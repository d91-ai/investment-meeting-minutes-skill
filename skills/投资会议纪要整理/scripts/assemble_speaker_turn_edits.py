#!/usr/bin/env python3
"""Assemble validated speaker-turn edits in source order as a main-owned working draft."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from collect_mas_artifacts import (
    artifact_context_errors,
    collect_artifact_files,
    merge_artifact_files,
)
from mas_task_lock import mas_task_lock
from validate_mas_artifacts import canonical_json_digest, file_sha256, validate_payload


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def assemble_speaker_turn_edits(
    task_dir: Path,
    output_path: Path | None = None,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    task_dir = task_dir.expanduser()
    artifact_dir = task_dir / "artifacts"
    receipt_path = artifact_dir / "editing_assembly_receipt.json"
    with mas_task_lock(task_dir, exclusive=True):
        bundle = read_json(task_dir / "mas_task_bundle.json")
        if not isinstance(bundle, dict):
            raise ValueError("MAS task bundle 顶层必须是 JSON object")
        manifest = bundle.get("speaker_turn_manifest")
        if not isinstance(manifest, dict):
            raise ValueError("当前 MAS run 未绑定 speaker_turn_manifest")
        artifacts, _, merge_errors, duplicates = merge_artifact_files(
            collect_artifact_files(artifact_dir)
        )
        errors = list(merge_errors)
        if duplicates:
            errors.append("存在重复 MAS artifact，不能组装 speaker turns")
        edit_types = sorted(
            str(shard.get("artifact_type") or "")
            for shard in manifest.get("shards", [])
            if isinstance(shard, dict)
        )
        missing = [artifact_type for artifact_type in edit_types if artifact_type not in artifacts]
        if missing:
            errors.append("缺少 speaker 编辑 artifact: " + ", ".join(missing))
        edit_artifacts = {
            artifact_type: artifacts[artifact_type]
            for artifact_type in edit_types
            if artifact_type in artifacts
        }
        validation = validate_payload({"artifacts": edit_artifacts}, required_artifacts=edit_types)
        errors.extend(str(item) for item in validation.get("errors", []))
        context_artifacts = {
            key: value
            for key, value in artifacts.items()
            if key != "editing_assembly_receipt"
        }
        errors.extend(artifact_context_errors(context_artifacts, bundle, task_dir))
        if errors:
            raise ValueError("; ".join(dict.fromkeys(errors)))

        edited_by_id: dict[str, dict[str, Any]] = {}
        for artifact in edit_artifacts.values():
            for turn in artifact.get("edited_turns", []):
                if isinstance(turn, dict):
                    edited_by_id[str(turn.get("turn_id") or "")] = turn
        ordered_source_turns = sorted(
            (turn for turn in manifest.get("turns", []) if isinstance(turn, dict)),
            key=lambda turn: int(turn.get("sequence") or 0),
        )
        lines = [
            "<!-- main-owned working draft; not the final meeting-minutes Markdown -->",
            "",
        ]
        ordered_turn_ids: list[str] = []
        for source_turn in ordered_source_turns:
            turn_id = str(source_turn.get("turn_id") or "")
            edited_turn = edited_by_id.get(turn_id)
            if not isinstance(edited_turn, dict):
                raise ValueError(f"组装时缺少 turn: {turn_id}")
            ordered_turn_ids.append(turn_id)
            lines.extend(
                [
                    f"<!-- turn_id={turn_id} sequence={source_turn.get('sequence')} -->",
                    f"### {source_turn.get('speaker_label')}",
                    "",
                    str(edited_turn.get("edited_text") or "").strip(),
                    "",
                ]
            )

        output_path = output_path or task_dir / "working" / "speaker-edited-body.md"
        output_path = output_path.expanduser()
        if not output_path.is_absolute():
            output_path = task_dir / output_path
        if (output_path.exists() or receipt_path.exists()) and not replace:
            raise ValueError("speaker 编辑工作稿或 assembly receipt 已存在；显式传入 --replace 后才能替换")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

        run_id = str(bundle.get("run_id") or "")
        receipt = {
            "run_id": run_id,
            "task_id": f"{run_id}:main:editing_assembly_receipt",
            "dispatch_phase": "editing",
            "artifact_owner": "Main Orchestrator",
            "artifact_type": "editing_assembly_receipt",
            "artifact": {
                "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
                "edit_artifact_digest": canonical_json_digest(edit_artifacts),
                "ordered_turn_ids": ordered_turn_ids,
                "assembled_draft_path": str(output_path),
                "assembled_draft_sha256": file_sha256(output_path),
                "status": "assembled",
            },
        }
        write_json(receipt_path, receipt)
        return {
            "ok": True,
            "working_draft": str(output_path),
            "receipt": str(receipt_path),
            "turn_count": len(ordered_turn_ids),
            "edit_artifact_count": len(edit_artifacts),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="按全局 sequence 组装 speaker 编辑结果并写入主流程凭据")
    parser.add_argument("task_dir", type=Path, help="MAS dispatch 目录")
    parser.add_argument("--out", type=Path, help="主流程工作稿路径")
    parser.add_argument("--replace", action="store_true", help="显式替换已有工作稿和 assembly receipt")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    try:
        result = assemble_speaker_turn_edits(args.task_dir, args.out, replace=args.replace)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        result = {"ok": False, "errors": [str(exc)]}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result.get("ok"):
        print(result["working_draft"])
    else:
        print(result["errors"][0], file=sys.stderr)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
