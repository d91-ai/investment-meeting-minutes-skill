#!/usr/bin/env python3
"""Generate derived summary/table artifacts from a human-confirmed final note."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_WORKSPACE_ROOT = (
    Path(os.environ["INVESTMENT_MINUTES_WORKSPACE"]).expanduser()
    if os.environ.get("INVESTMENT_MINUTES_WORKSPACE")
    else Path.home() / "Documents/会议纪要整理"
)
DEFAULT_FINAL_ROOT = DEFAULT_WORKSPACE_ROOT / "01 Projects/会议纪要"


def markdown_field(markdown: str, field: str, fallback: str = "") -> str:
    pattern = re.compile(rf"^\*\*{re.escape(field)}\*\*[:：]\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(markdown)
    return match.group(1).strip() if match else fallback


def markdown_title(markdown: str, fallback: str = "投资会议纪要") -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip() or fallback
    return fallback


def source_hash(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()[:16]


def number_tokens(text: str) -> list[str]:
    pattern = r"\d+(?:\.\d+)?\s*(?:%|％|亿|万|倍|个|只|元|块|年|月|日|pct|PE|G|T|MW|GW)?"
    return sorted(set(token.strip() for token in re.findall(pattern, text, flags=re.I) if token.strip()))[:12]


def split_final_note(markdown: str) -> list[dict[str, Any]]:
    body_start = markdown.find("## 一、发言整理")
    doubt_start = markdown.find("## 二、存疑与待确认")
    body = markdown[body_start:doubt_start if doubt_start >= 0 else len(markdown)] if body_start >= 0 else markdown
    segments: list[dict[str, Any]] = []
    current_speaker = "未标注发言人"
    current_target = ""
    current_board = ""
    buffer: list[str] = []

    def heading_text(line: str, prefix: str) -> str:
        value = line.removeprefix(prefix).strip()
        if value.startswith("【") and "】" in value:
            return value[1 : value.find("】")].strip()
        return value

    def flush() -> None:
        nonlocal buffer
        text = "\n".join(buffer).strip()
        if not text:
            return
        segments.append(
            {
                "speaker": current_speaker,
                "heading": current_target,
                "board": current_board,
                "target": current_target or "-",
                "text": text,
                "numbers": number_tokens(text),
                "has_doubt": "**" in text,
            }
        )
        buffer = []

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if line.startswith("### "):
            flush()
            current_speaker = line[4:].strip() or current_speaker
            current_target = ""
            current_board = ""
            continue
        if line.startswith("#### "):
            flush()
            current_target = heading_text(line, "#### ")
            current_board = ""
            continue
        if line.startswith("##### "):
            flush()
            current_board = heading_text(line, "##### ")
            continue
        if line.startswith("【") and "】" in line:
            flush()
            close = line.find("】")
            current_target = line[1:close].strip()
            rest = line[close + 1 :].strip()
            if rest:
                buffer.append(rest)
            continue
        if line.strip() and line.strip() != "---" and not line.startswith("## "):
            buffer.append(line)
    flush()
    return segments


def summarize_segments(segments: list[dict[str, Any]], limit: int = 12) -> list[str]:
    bullets: list[str] = []
    for item in segments:
        target = item.get("target") or "-"
        speaker = item.get("speaker") or "未标注发言人"
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        if not text:
            continue
        excerpt = text[:180] + ("..." if len(text) > 180 else "")
        bullets.append(f"- **{target}**（{speaker}）：{excerpt}")
        if len(bullets) >= limit:
            break
    return bullets


def target_table(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in segments:
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        rows.append(
            {
                "speaker": item.get("speaker") or "",
                "board": item.get("board") or "",
                "target": item.get("target") or "-",
                "view_excerpt": text[:220] + ("..." if len(text) > 220 else ""),
                "numbers": item.get("numbers") or [],
                "has_doubt": bool(item.get("has_doubt")),
            }
        )
    return rows


def speaker_rollup(segments: list[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for item in segments:
        speaker = str(item.get("speaker") or "未标注发言人")
        target = str(item.get("target") or "-")
        text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
        grouped[speaker].append(f"{target}: {text[:140]}{'...' if len(text) > 140 else ''}")
    return dict(grouped)


def render_markdown(payload: dict[str, Any]) -> str:
    table_rows = []
    for row in payload["target_table"]:
        table_rows.append(
            "| {speaker} | {board} | {target} | {view} | {numbers} | {doubt} |".format(
                speaker=str(row.get("speaker") or "").replace("|", "/"),
                board=str(row.get("board") or "").replace("|", "/"),
                target=str(row.get("target") or "-").replace("|", "/"),
                view=str(row.get("view_excerpt") or "").replace("|", "/"),
                numbers=", ".join(row.get("numbers") or []) or "-",
                doubt="是" if row.get("has_doubt") else "否",
            )
        )
    speaker_blocks = []
    for speaker, values in payload["speaker_rollup"].items():
        speaker_blocks.append(f"### {speaker}\n" + "\n".join(f"- {value}" for value in values[:12]))
    meta = payload["metadata"]
    return "\n".join(
        [
            f"# {meta['title']} - 终稿后结构化产物",
            "",
            f"**会议日期**：{meta['meeting_date']}",
            f"**会议类型**：{meta['meeting_type']}",
            f"**来源终稿**：{meta['source_path']}",
            f"**来源哈希**：{meta['source_hash']}",
            f"**生成时间**：{meta['generated_at']}",
            "",
            "## 一、结构化摘要",
            "",
            "\n".join(payload["summary_bullets"]) or "未抽取到摘要。",
            "",
            "## 二、标的观点表",
            "",
            "| 发言人 | 板块 | 标的 | 观点摘录 | 数字锚点 | 含存疑 |",
            "| --- | --- | --- | --- | --- | --- |",
            "\n".join(table_rows) if table_rows else "| - | - | - | - | - | - |",
            "",
            "## 三、发言人观点汇总",
            "",
            "\n\n".join(speaker_blocks) if speaker_blocks else "未抽取到发言人观点。",
            "",
        ]
    )


def generate_artifacts(
    final_note_path: Path,
    *,
    output_dir: Path | None = None,
    final_root: Path | None = None,
    require_final_root: bool = True,
) -> dict[str, Any]:
    path = final_note_path.expanduser().resolve()
    resolved_final_root = (final_root or DEFAULT_FINAL_ROOT).expanduser().resolve()
    if require_final_root and resolved_final_root not in path.parents and path.parent != resolved_final_root:
        raise ValueError(f"final note must be under {resolved_final_root}")
    markdown = path.read_text(encoding="utf-8")
    segments = split_final_note(markdown)
    generated_at = datetime.now().isoformat(timespec="seconds")
    target_dir = output_dir.expanduser().resolve() if output_dir else path.parent / ".derived"
    target_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {
            "title": markdown_title(markdown, path.stem),
            "meeting_date": markdown_field(markdown, "会议日期", ""),
            "meeting_type": markdown_field(markdown, "会议类型", "多人复盘会"),
            "source_path": str(path),
            "source_hash": source_hash(markdown),
            "generated_at": generated_at,
            "segment_count": len(segments),
        },
        "summary_bullets": summarize_segments(segments),
        "target_table": target_table(segments),
        "speaker_rollup": speaker_rollup(segments),
    }
    stem = path.stem
    json_path = target_dir / f"{stem}.post_review_artifacts.json"
    md_path = target_dir / f"{stem}.post_review_artifacts.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return {
        "ok": True,
        "source_path": str(path),
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "source_hash": payload["metadata"]["source_hash"],
        "segment_count": len(segments),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="从人工确认后的终稿生成结构化摘要和标的表格")
    parser.add_argument("final_note", help="Obsidian 正式归档 Markdown 路径")
    parser.add_argument("--output-dir", default="", help="默认写入终稿同目录 .derived/")
    parser.add_argument("--final-root", default=str(DEFAULT_FINAL_ROOT), help="正式纪要根目录；默认从 INVESTMENT_MINUTES_WORKSPACE 推导")
    parser.add_argument("--no-final-root-check", action="store_true", help="跳过终稿必须位于正式纪要根目录下的保护检查")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    result = generate_artifacts(
        Path(args.final_note),
        output_dir=Path(args.output_dir) if args.output_dir else None,
        final_root=Path(args.final_root),
        require_final_root=not args.no_final_root_check,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"generated: {result['markdown_path']}")
        print(f"generated: {result['json_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
