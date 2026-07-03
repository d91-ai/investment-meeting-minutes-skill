#!/usr/bin/env python3
"""
Preprocess Chinese investment meeting transcripts before final structuring.

What it does:
- removes obvious filler words and repeated verbal tics
- detects speaker blocks
- extracts likely symbol and company candidates
- emits a structured helper document for follow-up editing
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FILLER_PATTERNS = [
    (r"(就是说)+", ""),
    (r"(然后就是)+", "然后"),
    (r"(那个那个)+", "那个"),
    (r"(对对对|是是是|嗯嗯嗯)", ""),
    (r"(嗯|啊|呃|哦|哈)[，, ]*", ""),
    (r"^(那个|就是|然后|所以)[，, ]*", ""),
    (r"我跟你说[，, ]*", ""),
    (r"你知道吗?[，, ]*", ""),
    (r"怎么说呢[，, ]*", ""),
    (r"\s{2,}", " "),
]

SPEAKER_PATTERNS = [
    r"^(?P<speaker>[A-Za-z]{1,3}|发言人[A-Z]|主持人|分析师|嘉宾)[：:]\s*(?P<content>.+)$",
    r"^(?P<speaker>发言人\d+|[A-Za-z]{1,3}|主持人|分析师|嘉宾)\s+(?P<timestamp>\d{1,2}:\d{2}(?::\d{2})?)\s*(?P<content>.*)$",
    r"^(?P<speaker>[\u4e00-\u9fff]{1,8})[：:]\s*(?P<content>.+)$",
    r"^【(?P<speaker>[^】]{1,12})】\s*(?P<content>.+)$",
]

SYMBOL_PATTERNS = [
    r"\b\d{6}\.(?:SH|SZ|BJ)\b",
    r"\b\d{4,5}\.HK\b",
    r"\b[A-Z]{1,5}\b",
]

COMPANY_HINTS = [
    "股份",
    "集团",
    "科技",
    "医药",
    "银行",
    "证券",
    "保险",
    "基金",
    "控股",
    "能源",
    "电力",
    "半导体",
    "电子",
    "汽车",
]


def clean_text(text: str) -> str:
    cleaned = text
    for pattern, replacement in FILLER_PATTERNS:
        cleaned = re.sub(pattern, replacement, cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def detect_segments(text: str, preferred_speakers: list[str] | None) -> list[tuple[str, str]]:
    lines = [line.rstrip() for line in text.splitlines()]
    segments: list[tuple[str, str]] = []
    current_speaker = ""
    buffer: list[str] = []

    preferred_pattern = None
    if preferred_speakers:
        joined = "|".join(re.escape(name) for name in preferred_speakers)
        preferred_pattern = re.compile(
            rf"^(?P<speaker>{joined})[：:]\s*(?P<content>.+)$"
        )

    def flush() -> None:
        nonlocal buffer, current_speaker
        if buffer:
            speaker = current_speaker or "发言人A"
            segments.append((speaker, clean_text("\n".join(buffer))))
            buffer = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if buffer:
                buffer.append("")
            continue

        matched = None
        if preferred_pattern:
            matched = preferred_pattern.match(stripped)

        if not matched:
            for pattern in SPEAKER_PATTERNS:
                matched = re.match(pattern, stripped)
                if matched:
                    break

        if matched:
            flush()
            current_speaker = matched.group("speaker")
            content = matched.groupdict().get("content", "")
            if content:
                buffer.append(content)
        else:
            buffer.append(stripped)

    flush()
    return segments or [("发言人A", clean_text(text))]


def extract_candidates(text: str) -> tuple[list[str], list[str]]:
    symbols: set[str] = set()
    companies: set[str] = set()

    for pattern in SYMBOL_PATTERNS:
        symbols.update(re.findall(pattern, text))

    for hint in COMPANY_HINTS:
        companies.update(re.findall(rf"[\u4e00-\u9fff]{{2,8}}{re.escape(hint)}", text))

    return sorted(symbols), sorted(companies)


def build_output(segments: list[tuple[str, str]], source_text: str) -> str:
    symbols, companies = extract_candidates(source_text)
    lines = [
        "# 转录预处理结果",
        "",
        f"- 发言段数：{len(segments)}",
        f"- 疑似股票代码：{', '.join(symbols) if symbols else '未识别'}",
        f"- 疑似公司名称：{', '.join(companies[:20]) if companies else '未识别'}",
        "",
        "## 发言分段",
        "",
    ]

    for index, (speaker, content) in enumerate(segments, start=1):
        lines.extend(
            [
                f"### {index}. {speaker}",
                "",
                content or "无内容",
                "",
            ]
        )

    lines.extend(
        [
            "## 使用提醒",
            "",
            "- 这只是预处理结果，不是最终纪要。",
            "- 最终输出必须按每个人的真实发言顺序整理原文；只允许删除纯口水词、明显 ASR 噪声、无意义重复和重复起手式，不得总结、改写、改变人称或补充整理者判断。",
            "- 公司名称和股票代码必须在终稿阶段再次校验。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="预处理会议转录文本")
    parser.add_argument("input_file", nargs="?", help="输入文本路径")
    parser.add_argument("--stdin", action="store_true", help="从标准输入读取")
    parser.add_argument("--output", "-o", help="输出文件路径")
    parser.add_argument("--speakers", help="已知发言人列表，逗号分隔")
    args = parser.parse_args()

    if args.stdin:
        raw_text = sys.stdin.read()
    elif args.input_file:
        input_path = Path(args.input_file).expanduser()
        if not input_path.exists():
            print(f"输入文件不存在: {input_path}", file=sys.stderr)
            return 1
        raw_text = input_path.read_text(encoding="utf-8")
    else:
        parser.error("请提供输入文件或使用 --stdin")

    preferred_speakers = (
        [item.strip() for item in args.speakers.split(",") if item.strip()]
        if args.speakers
        else None
    )
    segments = detect_segments(raw_text, preferred_speakers)
    output = build_output(segments, raw_text)

    if args.output:
        Path(args.output).expanduser().write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
