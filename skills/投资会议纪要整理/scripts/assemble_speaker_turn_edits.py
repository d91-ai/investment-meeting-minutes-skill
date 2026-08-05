#!/usr/bin/env python3
"""Validate and order long-material package returns.

The output is process-only JSON. It deliberately does not create Markdown,
speaker headings, Q&A formatting, receipts, hashes, or meeting-type structure.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def assemble_returns(manifest: dict[str, Any], returns: Iterable[dict[str, Any]]) -> dict[str, Any]:
    if manifest.get("routing", {}).get("mode") != "sharded":
        raise ValueError("只有 sharded 长材料需要组装 package returns")
    turns = manifest.get("turns")
    packages = manifest.get("packages")
    if not isinstance(turns, list) or not isinstance(packages, list) or not packages:
        raise ValueError("manifest 缺少 turns 或 packages")

    turn_by_id = {str(turn.get("turn_id")): turn for turn in turns if isinstance(turn, dict)}
    expected_package_ids = [str(package.get("package_id")) for package in packages]
    return_by_package: dict[str, dict[str, Any]] = {}
    for payload in returns:
        if not isinstance(payload, dict):
            raise ValueError("package return 必须是 JSON object")
        package_id = str(payload.get("package_id") or "")
        if package_id not in expected_package_ids:
            raise ValueError(f"未知 package_id: {package_id}")
        if package_id in return_by_package:
            raise ValueError(f"重复 package return: {package_id}")
        return_by_package[package_id] = payload

    missing_packages = [item for item in expected_package_ids if item not in return_by_package]
    if missing_packages:
        raise ValueError("缺少 package return: " + ", ".join(missing_packages))

    ordered: list[dict[str, Any]] = []
    seen_turns: set[str] = set()
    for package in packages:
        package_id = str(package["package_id"])
        expected_turn_ids = [str(item) for item in package.get("turn_ids", [])]
        payload_turns = return_by_package[package_id].get("turns")
        if not isinstance(payload_turns, list):
            raise ValueError(f"{package_id}.turns 必须是 JSON array")
        returned_ids = [str(item.get("turn_id") or "") for item in payload_turns if isinstance(item, dict)]
        if returned_ids != expected_turn_ids:
            raise ValueError(f"{package_id} 的 turn_id 必须与 manifest 完全一致并保持顺序")
        for item in payload_turns:
            turn_id = str(item["turn_id"])
            if turn_id in seen_turns:
                raise ValueError(f"重复 turn_id: {turn_id}")
            seen_turns.add(turn_id)
            reference_text = item.get("reference_text")
            minutes_text = item.get("minutes_text")
            if not isinstance(reference_text, str) or not reference_text.strip():
                raise ValueError(f"{turn_id}.reference_text 必须是非空字符串")
            if not isinstance(minutes_text, str):
                raise ValueError(f"{turn_id}.minutes_text 必须是字符串")
            source_turn = turn_by_id[turn_id]
            ordered.append(
                {
                    "turn_id": turn_id,
                    "sequence": int(source_turn["sequence"]),
                    "speaker_label": str(source_turn["speaker_label"]),
                    "reference_text": reference_text.strip(),
                    "minutes_text": minutes_text.strip(),
                }
            )

    expected_all = [str(turn["turn_id"]) for turn in sorted(turns, key=lambda item: int(item["sequence"]))]
    returned_all = [str(turn["turn_id"]) for turn in ordered]
    if returned_all != expected_all:
        raise ValueError("组装结果未按来源顺序完整覆盖所有 turn")

    return {
        "schema_version": "1.0",
        "source_sha256": str(manifest.get("source_sha256") or ""),
        "turns": ordered,
        "coverage": {
            "complete": True,
            "turn_count": len(ordered),
            "duplicate_turns": [],
            "missing_turns": [],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="组装长材料 package returns 为有序 JSON 工作稿")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("returns", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        manifest = read_json(args.manifest)
        payloads = [read_json(path) for path in args.returns]
        result = assemble_returns(manifest, payloads)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
