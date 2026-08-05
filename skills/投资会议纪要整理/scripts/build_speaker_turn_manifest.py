#!/usr/bin/env python3
"""Parse explicit speaker turns and plan capacity-bounded long-material packages.

This script is intentionally mechanical. It does not clean prose, infer a
speaker, decide Q&A relationships, or judge what information may be removed.
The main model reviews and may adjust every package boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

DEFAULT_TARGET_CHARS = 12_000
DEFAULT_HARD_LIMIT_CHARS = 16_000

ANONYMOUS_LABEL_RE = re.compile(
    r"^(?P<label>(?:说话人|发言人|Speaker)\s*(?P<id>[A-Za-z0-9甲乙丙丁戊己庚辛壬癸]+))"
    r"(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*(?:[：:]\s*(?P<content>.*))?$",
    re.IGNORECASE,
)
BRACKET_LABEL_RE = re.compile(
    r"^[【\[](?P<label>[^】\]]{1,24})[】\]]\s*(?:[：:]\s*)?(?P<content>.*)$"
)
DEFAULT_ROLE_LABELS = {
    "主持人",
    "分析师",
    "研究员",
    "基金经理",
    "嘉宾",
    "专家",
    "管理层",
    "负责人",
    "董秘",
    "提问者",
}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize_anonymous(label: str) -> str:
    compact = re.sub(r"\s+", "", label)
    matched = re.fullmatch(r"(?:说话人|发言人|speaker)(.+)", compact, re.IGNORECASE)
    return f"发言人{matched.group(1)}" if matched else label.strip()


def _match_speaker_line(line: str, known_speakers: set[str]) -> tuple[str, int | None] | None:
    content = line.rstrip("\r\n")
    match_content = re.sub(
        r"^\[(?:\d{1,3}:\d{2}|\d{1,2}:\d{2}:\d{2})\]\s*",
        "",
        content.strip(),
        count=1,
    )
    matched = ANONYMOUS_LABEL_RE.fullmatch(match_content)
    if matched:
        label = _normalize_anonymous(matched.group("label"))
        inline = matched.group("content")
        if inline:
            return label, content.rfind(inline)
        return label, None

    matched = BRACKET_LABEL_RE.fullmatch(match_content)
    if matched:
        label = matched.group("label").strip()
        if label not in DEFAULT_ROLE_LABELS and label not in known_speakers:
            return None
        inline = matched.group("content")
        if inline:
            return label, content.rfind(inline)
        return label, None

    labels = sorted(DEFAULT_ROLE_LABELS | known_speakers, key=len, reverse=True)
    if labels:
        role_match = re.fullmatch(
            rf"(?P<label>{'|'.join(re.escape(item) for item in labels)})\s*[：:]\s*(?P<content>.*)",
            match_content,
        )
        if role_match:
            inline = role_match.group("content")
            return role_match.group("label"), content.rfind(inline) if inline else None
    return None


def _trimmed_span(source: str, start: int, end: int) -> tuple[int, int, str] | None:
    raw = source[start:end]
    if not raw.strip():
        return None
    left = len(raw) - len(raw.lstrip())
    right = len(raw.rstrip())
    actual_start = start + left
    actual_end = start + right
    return actual_start, actual_end, source[actual_start:actual_end]


def parse_explicit_turns(source: str, known_speakers: Iterable[str] = ()) -> list[dict[str, Any]]:
    known = {item.strip() for item in known_speakers if item.strip()}
    raw_turns: list[tuple[str, int, int]] = []
    current_speaker = "发言人1"
    current_start = 0
    offset = 0

    for line in source.splitlines(keepends=True):
        line_start = offset
        line_end = offset + len(line)
        matched = _match_speaker_line(line, known)
        if matched is not None:
            previous = _trimmed_span(source, current_start, line_start)
            if previous is not None:
                raw_turns.append((current_speaker, previous[0], previous[1]))
            current_speaker, inline_index = matched
            current_start = line_end if inline_index is None else line_start + inline_index
        offset = line_end

    final = _trimmed_span(source, current_start, len(source))
    if final is not None:
        raw_turns.append((current_speaker, final[0], final[1]))
    if not raw_turns and source.strip():
        trimmed = _trimmed_span(source, 0, len(source))
        assert trimmed is not None
        raw_turns.append(("发言人1", trimmed[0], trimmed[1]))

    turns: list[dict[str, Any]] = []
    for sequence, (speaker, start, end) in enumerate(raw_turns, start=1):
        text = source[start:end]
        turns.append(
            {
                "turn_id": f"turn_{sequence:04d}",
                "sequence": sequence,
                "speaker_label": speaker,
                "text": text,
                "char_count": len(text),
                "source_span": {"start_char": start, "end_char": end},
                "text_sha256": sha256_text(text),
            }
        )
    return turns


def _split_point(text: str, hard_limit: int) -> int:
    window = text[: hard_limit + 1]
    minimum = max(1, hard_limit // 2)
    candidates: list[int] = []
    for marker in ("\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";"):
        point = window.rfind(marker)
        if point >= minimum:
            candidates.append(point + len(marker))
    return max(candidates) if candidates else hard_limit


def split_oversized_turns(turns: list[dict[str, Any]], hard_limit: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for turn in turns:
        text = str(turn["text"])
        if len(text) <= hard_limit:
            result.append(dict(turn))
            continue
        relative = 0
        part = 1
        while relative < len(text):
            remaining = text[relative:]
            size = len(remaining) if len(remaining) <= hard_limit else _split_point(remaining, hard_limit)
            piece = remaining[:size]
            leading = len(piece) - len(piece.lstrip())
            trailing = len(piece.rstrip())
            if trailing > leading:
                piece_start = relative + leading
                piece_end = relative + trailing
                shown = text[piece_start:piece_end]
                absolute_start = int(turn["source_span"]["start_char"]) + piece_start
                result.append(
                    {
                        **turn,
                        "text": shown,
                        "char_count": len(shown),
                        "source_span": {
                            "start_char": absolute_start,
                            "end_char": absolute_start + len(shown),
                        },
                        "text_sha256": sha256_text(shown),
                        "split_part": part,
                    }
                )
                part += 1
            relative += max(1, size)

    for sequence, turn in enumerate(result, start=1):
        turn["turn_id"] = f"turn_{sequence:04d}"
        turn["sequence"] = sequence
    return result


def _packages_from_breaks(
    turns: list[dict[str, Any]], starts: list[str], hard_limit: int
) -> list[list[dict[str, Any]]]:
    ids = [str(turn["turn_id"]) for turn in turns]
    if not starts or starts[0] != ids[0]:
        raise ValueError("package_breaks 必须以第一个 turn_id 开始")
    if len(starts) != len(set(starts)) or any(item not in ids for item in starts):
        raise ValueError("package_breaks 包含重复或未知 turn_id")
    indexes = sorted(ids.index(item) for item in starts)
    if [ids[index] for index in indexes] != starts:
        raise ValueError("package_breaks 必须按来源顺序排列")
    packages: list[list[dict[str, Any]]] = []
    for index, start in enumerate(indexes):
        end = indexes[index + 1] if index + 1 < len(indexes) else len(turns)
        package = turns[start:end]
        if sum(int(turn["char_count"]) for turn in package) > hard_limit:
            raise ValueError("模型指定的 package 超过 16,000 字硬上限")
        packages.append(package)
    return packages


def package_turns(
    turns: list[dict[str, Any]],
    target_chars: int = DEFAULT_TARGET_CHARS,
    hard_limit_chars: int = DEFAULT_HARD_LIMIT_CHARS,
    package_breaks: list[str] | None = None,
) -> list[dict[str, Any]]:
    if target_chars <= 0 or hard_limit_chars < target_chars:
        raise ValueError("字符阈值必须满足 0 < target_chars <= hard_limit_chars")
    if any(int(turn["char_count"]) > hard_limit_chars for turn in turns):
        raise ValueError("存在未拆分的超限 turn")

    if package_breaks is not None:
        raw_packages = _packages_from_breaks(turns, package_breaks, hard_limit_chars)
    else:
        raw_packages: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_chars = 0
        for turn in turns:
            turn_chars = int(turn["char_count"])
            if current and current_chars + turn_chars > target_chars:
                raw_packages.append(current)
                current = []
                current_chars = 0
            current.append(turn)
            current_chars += turn_chars
        if current:
            raw_packages.append(current)

    packages: list[dict[str, Any]] = []
    for sequence, package in enumerate(raw_packages, start=1):
        char_count = sum(int(turn["char_count"]) for turn in package)
        if char_count > hard_limit_chars:
            raise ValueError("生成的 package 超过 hard limit")
        packages.append(
            {
                "package_id": f"package_{sequence:03d}",
                "sequence": sequence,
                "turn_ids": [str(turn["turn_id"]) for turn in package],
                "char_count": char_count,
                "over_target": char_count > target_chars,
                "boundary_review_required": sequence < len(raw_packages),
            }
        )
    return packages


def build_manifest(
    source: str,
    *,
    source_name: str = "source.txt",
    target_chars: int = DEFAULT_TARGET_CHARS,
    hard_limit_chars: int = DEFAULT_HARD_LIMIT_CHARS,
    known_speakers: Iterable[str] = (),
    package_breaks: list[str] | None = None,
) -> dict[str, Any]:
    if not source.strip():
        raise ValueError("来源文本为空")
    turns = parse_explicit_turns(source, known_speakers)
    turns = split_oversized_turns(turns, hard_limit_chars)
    source_char_count = len(source)
    mode = "direct" if source_char_count <= target_chars else "sharded"
    packages = (
        []
        if mode == "direct"
        else package_turns(turns, target_chars, hard_limit_chars, package_breaks)
    )
    return {
        "schema_version": "1.0",
        "source_name": Path(source_name).name,
        "source_sha256": sha256_text(source),
        "source_char_count": source_char_count,
        "routing": {
            "mode": mode,
            "target_chars": target_chars,
            "hard_limit_chars": hard_limit_chars,
            "total_length_is_unbounded": True,
        },
        "turns": turns,
        "packages": packages,
        "requires_semantic_boundary_review": len(packages) > 1,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="解析显式发言轮次并规划长材料工作包")
    parser.add_argument("source", type=Path, help="UTF-8 来源文本")
    parser.add_argument("--out", required=True, type=Path, help="manifest JSON 输出路径")
    parser.add_argument("--target-chars", type=int, default=DEFAULT_TARGET_CHARS)
    parser.add_argument("--hard-limit-chars", type=int, default=DEFAULT_HARD_LIMIT_CHARS)
    parser.add_argument("--known-speaker", action="append", default=[])
    parser.add_argument("--package-breaks", type=Path, help="模型确认的 package 起始 turn_id JSON array")
    args = parser.parse_args()

    try:
        source = args.source.read_text(encoding="utf-8")
        package_breaks = None
        if args.package_breaks:
            loaded = json.loads(args.package_breaks.read_text(encoding="utf-8"))
            if not isinstance(loaded, list) or not all(isinstance(item, str) for item in loaded):
                raise ValueError("package_breaks 必须是 turn_id JSON array")
            package_breaks = loaded
        manifest = build_manifest(
            source,
            source_name=args.source.name,
            target_chars=args.target_chars,
            hard_limit_chars=args.hard_limit_chars,
            known_speakers=args.known_speaker,
            package_breaks=package_breaks,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "errors": [str(exc)]}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
