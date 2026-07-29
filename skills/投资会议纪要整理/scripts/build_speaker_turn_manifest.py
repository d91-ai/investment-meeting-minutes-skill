#!/usr/bin/env python3
"""Build a deterministic, source-faithful speaker-turn editing manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from process_transcript import EXPLICIT_SPEAKER_PATTERNS, anonymous_speaker_key

SCHEMA_VERSION = "1.0"
DEFAULT_MAX_CHARS = 12_000


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256_text(payload)


def detect_source_turns(text: str) -> list[tuple[str, str]]:
    """Split speaker turns without applying filler removal or prose editing."""
    turns: list[tuple[str, str]] = []
    current_speaker = ""
    buffer: list[str] = []
    anonymous_labels: dict[str, str] = {}

    def flush() -> None:
        nonlocal buffer
        content = "\n".join(buffer).strip()
        if content:
            turns.append((current_speaker or "发言人1", content))
        buffer = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if buffer:
                buffer.append("")
            continue

        matched = None
        anonymous = False
        for pattern, is_anonymous in EXPLICIT_SPEAKER_PATTERNS:
            matched = pattern.match(stripped)
            if matched:
                anonymous = is_anonymous
                break

        if not matched:
            buffer.append(line)
            continue

        flush()
        raw_speaker = matched.group("speaker").strip()
        if anonymous:
            speaker_key = anonymous_speaker_key(raw_speaker)
            current_speaker = anonymous_labels.setdefault(
                speaker_key,
                f"发言人{len(anonymous_labels) + 1}",
            )
        else:
            current_speaker = raw_speaker

        content = matched.groupdict().get("content", "")
        if content:
            buffer.append(content)

    flush()
    return turns


def build_turns(source_text: str) -> list[dict[str, Any]]:
    detected = detect_source_turns(source_text)
    if not detected:
        raise ValueError("transcript 未检测到非空发言内容")

    speaker_ids: dict[str, str] = {}
    turns: list[dict[str, Any]] = []
    for sequence, (speaker_label, text) in enumerate(detected, start=1):
        speaker_id = speaker_ids.setdefault(
            speaker_label,
            f"speaker_{len(speaker_ids) + 1:03d}",
        )
        turns.append(
            {
                "turn_id": f"turn_{sequence:06d}",
                "sequence": sequence,
                "speaker_id": speaker_id,
                "speaker_label": speaker_label,
                "source_sha256": sha256_text(text),
                "text": text,
            }
        )
    return turns


def build_shards(
    turns: list[dict[str, Any]],
    *,
    source_name: str,
    source_sha256: str,
    max_chars: int,
) -> list[dict[str, Any]]:
    if max_chars <= 0:
        raise ValueError("--max-chars 必须大于 0")

    turns_by_speaker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    speaker_order: list[str] = []
    for turn in turns:
        speaker_id = str(turn["speaker_id"])
        if speaker_id not in turns_by_speaker:
            speaker_order.append(speaker_id)
        turns_by_speaker[speaker_id].append(turn)

    shards: list[dict[str, Any]] = []
    for speaker_id in speaker_order:
        speaker_turns = turns_by_speaker[speaker_id]
        parts: list[list[dict[str, Any]]] = []
        current_part: list[dict[str, Any]] = []
        current_chars = 0

        for turn in speaker_turns:
            turn_chars = len(str(turn["text"]))
            if current_part and current_chars + turn_chars > max_chars:
                parts.append(current_part)
                current_part = []
                current_chars = 0
            current_part.append(turn)
            current_chars += turn_chars

        if current_part:
            parts.append(current_part)

        for part_number, part_turns in enumerate(parts, start=1):
            artifact_type = (
                f"speaker_turn_edit__{speaker_id}__part_{part_number:02d}"
            )
            speaker_label = str(part_turns[0]["speaker_label"])
            input_payload = {
                "source_name": source_name,
                "source_sha256": source_sha256,
                "speaker_id": speaker_id,
                "speaker_label": speaker_label,
                "turns": part_turns,
            }
            shards.append(
                {
                    "shard_id": artifact_type,
                    "artifact_type": artifact_type,
                    "speaker_id": speaker_id,
                    "speaker_label": speaker_label,
                    "part": part_number,
                    "turn_ids": [turn["turn_id"] for turn in part_turns],
                    "turn_count": len(part_turns),
                    "char_count": sum(len(str(turn["text"])) for turn in part_turns),
                    "input_sha256": canonical_sha256(input_payload),
                }
            )

    return shards


def build_manifest(source_path: Path, source_text: str, max_chars: int) -> dict[str, Any]:
    source_sha256 = sha256_text(source_text)
    turns = build_turns(source_text)
    shards = build_shards(
        turns,
        source_name=source_path.name,
        source_sha256=source_sha256,
        max_chars=max_chars,
    )
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "speaker_turn_manifest",
        "source_name": source_path.name,
        "source_sha256": source_sha256,
        "turn_count": len(turns),
        "shard_count": len(shards),
        "turns": turns,
        "shards": shards,
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按发言人生成可并行编辑的 speaker-turn manifest。"
    )
    parser.add_argument("input", type=Path, help="UTF-8 原始 transcript 路径")
    parser.add_argument("--out", type=Path, required=True, help="manifest JSON 输出路径")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help=f"单 shard 最大字符数（默认 {DEFAULT_MAX_CHARS}；不拆分单个 turn）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        raw_bytes = args.input.read_bytes()
        if raw_bytes.startswith(b"\xef\xbb\xbf"):
            raise ValueError("输入文件必须为 UTF-8 without BOM")
        source_text = raw_bytes.decode("utf-8")
        manifest = build_manifest(args.input, source_text, args.max_chars)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "out": str(args.out),
                "turn_count": manifest["turn_count"],
                "shard_count": manifest["shard_count"],
                "manifest_sha256": manifest["manifest_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
