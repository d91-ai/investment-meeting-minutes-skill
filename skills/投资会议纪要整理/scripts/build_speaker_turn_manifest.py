#!/usr/bin/env python3
"""Build a deterministic, source-faithful speaker-turn editing manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from process_transcript import EXPLICIT_SPEAKER_PATTERNS, anonymous_speaker_key

SCHEMA_VERSION = "1.0"
DEFAULT_MAX_CHARS = 12_000
HARD_MAX_CHARS = 16_000
STANDALONE_GROUP_SPEAKER = re.compile(
    r"^[A-Za-z0-9\u4e00-\u9fff·+&（）()/-]{1,16}组$"
)
MARKDOWN_GROUP_SPEAKER = re.compile(
    r"^#{1,6}\s+(?P<label>[A-Za-z0-9\u4e00-\u9fff·+&（）()/-]{1,16}组)\s*#*\s*$"
)
STANDALONE_ANONYMOUS_SPEAKER = re.compile(
    r"^(?P<speaker>(?:Speaker|发言人|说话人)\s*(?:\d+|[A-Za-z]+|[甲乙丙丁戊己庚辛壬癸]))\s*[：:]?\s*$",
    re.IGNORECASE,
)


def group_speaker_label(line: str) -> str:
    stripped = line.strip()
    if STANDALONE_GROUP_SPEAKER.fullmatch(stripped):
        return stripped
    matched = MARKDOWN_GROUP_SPEAKER.fullmatch(stripped)
    return str(matched.group("label")) if matched else ""


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


def normalized_anonymous_label(value: str) -> str:
    normalized = re.sub(r"\s+", "", value.strip())
    normalized = re.sub(r"^说话人", "发言人", normalized)
    return normalized


def detect_source_turns(text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Split speaker turns without applying filler removal or prose editing."""
    turns: list[dict[str, Any]] = []
    current_speaker = ""
    buffer_start: int | None = None
    buffer_end: int | None = None
    anonymous_labels: dict[str, str] = {}
    source_lines = text.splitlines(keepends=True)
    group_labels = {
        label
        for line in source_lines
        if (label := group_speaker_label(line))
    }
    repeated_group_layout = len(group_labels) >= 2

    boundary_count = 0
    standalone_boundary_count = 0
    inline_boundary_count = 0
    unlabeled_prefix_chars = 0

    def flush() -> None:
        nonlocal buffer_start, buffer_end
        if buffer_start is None or buffer_end is None:
            return
        raw_content = text[buffer_start:buffer_end]
        leading = len(raw_content) - len(raw_content.lstrip())
        trailing = len(raw_content) - len(raw_content.rstrip())
        start_char = buffer_start + leading
        end_char = buffer_end - trailing
        content = text[start_char:end_char]
        if content:
            turns.append(
                {
                    "speaker_label": current_speaker or "发言人1",
                    "text": content,
                    "source_span": {"start_char": start_char, "end_char": end_char},
                }
            )
        buffer_start = None
        buffer_end = None

    offset = 0
    for line in source_lines:
        line_start = offset
        line_end = offset + len(line)
        offset = line_end
        stripped = line.strip()
        if not stripped:
            if buffer_start is not None:
                buffer_end = line_end
            continue

        group_label = group_speaker_label(stripped)
        if repeated_group_layout and group_label:
            flush()
            current_speaker = group_label
            boundary_count += 1
            standalone_boundary_count += 1
            continue

        standalone = STANDALONE_ANONYMOUS_SPEAKER.fullmatch(stripped)
        if standalone:
            flush()
            raw_speaker = normalized_anonymous_label(standalone.group("speaker"))
            speaker_key = anonymous_speaker_key(raw_speaker)
            current_speaker = anonymous_labels.setdefault(
                speaker_key,
                f"发言人{len(anonymous_labels) + 1}",
            )
            boundary_count += 1
            standalone_boundary_count += 1
            continue

        matched = None
        anonymous = False
        for pattern, is_anonymous in EXPLICIT_SPEAKER_PATTERNS:
            matched = pattern.match(stripped)
            if matched:
                anonymous = is_anonymous
                break

        if not matched:
            if buffer_start is None:
                if not current_speaker:
                    unlabeled_prefix_chars += len(stripped)
                buffer_start = line_start
            buffer_end = line_end
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
        boundary_count += 1
        inline_boundary_count += 1

        content = matched.groupdict().get("content", "")
        if content:
            stripped_start = line_start + (len(line) - len(line.lstrip()))
            content_start = stripped_start + matched.start("content")
            content_end = stripped_start + matched.end("content")
            buffer_start = content_start
            buffer_end = content_end

    flush()
    exact_spans = all(
        text[int(turn["source_span"]["start_char"]):int(turn["source_span"]["end_char"])] == turn["text"]
        for turn in turns
    )
    profile = {
        "boundary_count": boundary_count,
        "standalone_boundary_count": standalone_boundary_count,
        "inline_boundary_count": inline_boundary_count,
        "speaker_count": len({str(turn["speaker_label"]) for turn in turns}),
        "unlabeled_prefix_chars": unlabeled_prefix_chars,
        "structure_reliable": bool(turns) and boundary_count > 0 and unlabeled_prefix_chars == 0 and exact_spans,
        "lossless_renderable": bool(turns) and exact_spans,
    }
    return turns, profile


def split_oversized_text(text: str, max_chars: int) -> list[tuple[str, int, int]]:
    """Split an oversized turn at paragraph, then sentence, boundaries."""
    if len(text) <= max_chars:
        return [(text, 0, len(text))]

    chunks: list[tuple[str, int, int]] = []
    cursor = 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text):
            break
        limit = min(len(text), cursor + max_chars)
        boundary = limit
        if limit < len(text):
            window = text[cursor:limit]
            paragraph_boundary = max(window.rfind("\n\n"), window.rfind("\r\n\r\n"))
            line_boundary = window.rfind("\n")
            sentence_boundary = max((window.rfind(marker) + 1 for marker in "。！？!?;；"), default=0)
            candidates = [
                value
                for value in (paragraph_boundary + 2, line_boundary + 1, sentence_boundary)
                if value >= max_chars // 2
            ]
            if candidates:
                boundary = cursor + max(candidates)
        chunk_end = boundary
        while chunk_end > cursor and text[chunk_end - 1].isspace():
            chunk_end -= 1
        chunk = text[cursor:chunk_end]
        if chunk:
            chunks.append((chunk, cursor, chunk_end))
        cursor = boundary
    return chunks


def build_turns(source_text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[dict[str, Any]]:
    detected, _ = detect_source_turns(source_text)
    if not detected:
        raise ValueError("transcript 未检测到非空发言内容")

    speaker_ids: dict[str, str] = {}
    turns: list[dict[str, Any]] = []
    split_turns: list[tuple[str, str, dict[str, int]]] = []
    for detected_turn in detected:
        speaker_label = str(detected_turn["speaker_label"])
        text = str(detected_turn["text"])
        base_start = int(detected_turn["source_span"]["start_char"])
        split_turns.extend(
            (
                speaker_label,
                chunk,
                {"start_char": base_start + local_start, "end_char": base_start + local_end},
            )
            for chunk, local_start, local_end in split_oversized_text(text, max_chars)
        )
    for sequence, (speaker_label, text, source_span) in enumerate(split_turns, start=1):
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
                "source_span": source_span,
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
    if max_chars > HARD_MAX_CHARS:
        raise ValueError(f"--max-chars 不得超过硬上限 {HARD_MAX_CHARS}")

    shards: list[dict[str, Any]] = []
    current_part: list[dict[str, Any]] = []
    current_chars = 0

    def flush() -> None:
        nonlocal current_part, current_chars
        if not current_part:
            return
        package_number = len(shards) + 1
        artifact_type = f"speaker_turn_edit__package_{package_number:03d}"
        speaker_ids = list(
            dict.fromkeys(str(turn["speaker_id"]) for turn in current_part)
        )
        speaker_labels = list(
            dict.fromkeys(str(turn["speaker_label"]) for turn in current_part)
        )
        input_payload = {
            "source_name": source_name,
            "source_sha256": source_sha256,
            "speaker_ids": speaker_ids,
            "speaker_labels": speaker_labels,
            "turns": current_part,
        }
        shards.append(
            {
                "shard_id": artifact_type,
                "artifact_type": artifact_type,
                "speaker_ids": speaker_ids,
                "speaker_labels": speaker_labels,
                "package": package_number,
                "turn_ids": [turn["turn_id"] for turn in current_part],
                "turn_count": len(current_part),
                "char_count": current_chars,
                "input_sha256": canonical_sha256(input_payload),
            }
        )
        current_part = []
        current_chars = 0

    for turn in turns:
        turn_chars = len(str(turn["text"]))
        if current_part and current_chars + turn_chars > max_chars:
            flush()
        current_part.append(turn)
        current_chars += turn_chars
    flush()

    return shards


def build_manifest(source_path: Path, source_text: str, max_chars: int) -> dict[str, Any]:
    source_sha256 = sha256_text(source_text)
    if max_chars <= 0:
        raise ValueError("--max-chars 必须大于 0")
    if max_chars > HARD_MAX_CHARS:
        raise ValueError(f"--max-chars 不得超过硬上限 {HARD_MAX_CHARS}")
    _, source_profile = detect_source_turns(source_text)
    turns = build_turns(source_text, max_chars=max_chars)
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
        "source_profile": source_profile,
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
        help=(
            f"单工作包目标上限（默认 {DEFAULT_MAX_CHARS}，硬上限 "
            f"{HARD_MAX_CHARS}；只打包相邻完整 turns，超长 turn 优先按自然段拆分）"
        ),
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
