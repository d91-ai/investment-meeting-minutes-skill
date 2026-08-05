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

MINUTES_SEGMENT_KINDS = {"question", "answer", "paragraph"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_reference_segments(
    item: dict[str, Any], turn_id: str, default_speaker: str
) -> list[dict[str, str]]:
    raw_segments = item.get("reference_segments")
    if raw_segments is None:
        raise ValueError(f"{turn_id}.reference_segments 必须是 JSON array")
    if not isinstance(raw_segments, list):
        raise ValueError(f"{turn_id}.reference_segments 必须是 JSON array")

    normalized: list[dict[str, str]] = []
    for index, segment in enumerate(raw_segments, start=1):
        if not isinstance(segment, dict):
            raise ValueError(f"{turn_id}.reference_segments[{index}] 必须是 JSON object")
        speaker_label = segment.get("speaker_label")
        text = segment.get("text")
        if not isinstance(speaker_label, str) or not speaker_label.strip():
            raise ValueError(
                f"{turn_id}.reference_segments[{index}].speaker_label 必须是非空字符串"
            )
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                f"{turn_id}.reference_segments[{index}].text 必须是非空字符串"
            )
        normalized.append(
            {
                "speaker_label": speaker_label.strip(),
                "text": text.strip(),
            }
        )
    return normalized


def _normalize_minutes_segments(
    item: dict[str, Any], turn_id: str, default_speaker: str
) -> list[dict[str, str]]:
    raw_segments = item.get("minutes_segments")
    if raw_segments is None:
        raise ValueError(f"{turn_id}.minutes_segments 必须是 JSON array")
    if not isinstance(raw_segments, list):
        raise ValueError(f"{turn_id}.minutes_segments 必须是 JSON array")

    normalized: list[dict[str, str]] = []
    for index, segment in enumerate(raw_segments, start=1):
        if not isinstance(segment, dict):
            raise ValueError(f"{turn_id}.minutes_segments[{index}] 必须是 JSON object")
        kind = segment.get("kind")
        speaker_label = segment.get("speaker_label", default_speaker)
        text = segment.get("text")
        if not isinstance(kind, str) or kind not in MINUTES_SEGMENT_KINDS:
            raise ValueError(
                f"{turn_id}.minutes_segments[{index}].kind 必须是 question/answer/paragraph"
            )
        if not isinstance(text, str) or not text.strip():
            raise ValueError(
                f"{turn_id}.minutes_segments[{index}].text 必须是非空字符串"
            )
        if not isinstance(speaker_label, str) or not speaker_label.strip():
            raise ValueError(
                f"{turn_id}.minutes_segments[{index}].speaker_label 必须是非空字符串"
            )
        normalized.append(
            {
                "kind": str(kind),
                "speaker_label": speaker_label.strip(),
                "text": text.strip(),
            }
        )
    return normalized


def assemble_returns(manifest: dict[str, Any], returns: Iterable[dict[str, Any]]) -> dict[str, Any]:
    turns = manifest.get("turns")
    packages = manifest.get("packages")
    if not isinstance(turns, list) or not isinstance(packages, list) or not packages:
        raise ValueError("manifest 缺少 turns 或 packages")

    turn_by_id = {str(turn.get("turn_id")): turn for turn in turns if isinstance(turn, dict)}
    if len(turn_by_id) != len(turns):
        raise ValueError("manifest.turns 包含重复或无效 turn_id")
    expected_package_ids = [str(package.get("package_id")) for package in packages]
    if len(expected_package_ids) != len(set(expected_package_ids)):
        raise ValueError("manifest.packages 包含重复 package_id")
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
        if any(not isinstance(item, dict) for item in payload_turns):
            raise ValueError(f"{package_id}.turns 的每项必须是 JSON object")
        returned_ids = [str(item.get("turn_id") or "") for item in payload_turns]
        if returned_ids != expected_turn_ids:
            raise ValueError(f"{package_id} 的 turn_id 必须与 manifest 完全一致并保持顺序")
        for item in payload_turns:
            turn_id = str(item["turn_id"])
            if turn_id in seen_turns:
                raise ValueError(f"重复 turn_id: {turn_id}")
            seen_turns.add(turn_id)
            source_turn = turn_by_id[turn_id]
            default_speaker = str(source_turn["speaker_label"])
            reference_segments = _normalize_reference_segments(item, turn_id, default_speaker)
            minutes_segments = _normalize_minutes_segments(item, turn_id, default_speaker)
            reference_omission_reason = item.get("reference_omission_reason")
            minutes_omission_reason = item.get("minutes_omission_reason")
            if not reference_segments and (
                not isinstance(reference_omission_reason, str) or not reference_omission_reason.strip()
            ):
                raise ValueError(
                    f"{turn_id}.reference_segments 为空时必须提供 reference_omission_reason"
                )
            if not minutes_segments and (
                not isinstance(minutes_omission_reason, str) or not minutes_omission_reason.strip()
            ):
                raise ValueError(
                    f"{turn_id}.minutes_segments 为空时必须提供 minutes_omission_reason"
                )
            normalized_turn = {
                "package_id": package_id,
                "turn_id": turn_id,
                "sequence": int(source_turn["sequence"]),
                "speaker_label": default_speaker,
                "reference_segments": reference_segments,
                "minutes_segments": minutes_segments,
            }
            if isinstance(reference_omission_reason, str) and reference_omission_reason.strip():
                normalized_turn["reference_omission_reason"] = reference_omission_reason.strip()
            if isinstance(minutes_omission_reason, str) and minutes_omission_reason.strip():
                normalized_turn["minutes_omission_reason"] = minutes_omission_reason.strip()
            ordered.append(
                normalized_turn
            )

    expected_all = [str(turn["turn_id"]) for turn in sorted(turns, key=lambda item: int(item["sequence"]))]
    returned_all = [str(turn["turn_id"]) for turn in ordered]
    if returned_all != expected_all:
        raise ValueError("组装结果未按来源顺序完整覆盖所有 turn")

    return {
        "schema_version": "1.0",
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
