#!/usr/bin/env python3
"""Validate objective Markdown structure for investment meeting minutes."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

REQUIRED_METADATA_FIELDS = ["会议日期", "整理时间", "会议标题", "会议类型", "会议系列"]
MEETING_TYPES = {"多人复盘会", "公司交流", "专家交流"}
MEETING_TYPE_ALIASES = {"上市公司交流": "公司交流"}
QUESTION_LINE_RE = re.compile(r"^\*\*【[^】\n]+】\*\*\s*$", re.MULTILINE)
ALLOWED_AMBIGUITY_HEADERS = {
    ("原始表述", "当前判断", "候选项", "人工确认"),
    ("时间戳", "原始表述", "当前判断", "候选项", "人工确认"),
}
ALLOWED_CORRECTION_HEADERS = ("原始表述", "校对后", "依据")
PLACEHOLDER_VALUES = {"", "-", "无", "暂无", "无存疑", "暂无存疑", "none", "n/a"}
FORBIDDEN_PATTERNS = [
    r"AI结构化总结",
    r"标的汇总表",
    r"(?m)^##+\s*(?:摘要|投研摘要|整理者总结|投资结论|核心结论)\s*$",
    r"##\s*(?:四|五|六|七|八|九|十)[、.．]",
    r"处理说明",
    r"正常生产不增加全文二审",
    r"(?i)(?:对\s*)?skill\s*调整(?:的)?(?:提示|prompt|指令)?",
    r"(?m)^\*\*输入来源\*\*[:：]",
    r"(?m)^\*\*整理说明\*\*[:：]",
    r"Tool Call|skill_name|Traceback|Observation:",
    r"(?m)^###\s*问答\s*\d+",
    r"(?im)^\s*(?:Q|提问|问)[:：]",
    r"(?im)^\s*A[:：]",
]
FORBIDDEN_ESCAPE_HEADINGS = {
    "发言片段",
    "未归类",
    "主题整理",
    "内容摘要",
    "观点汇总",
    "核心观点",
    "整体判断",
    "整理者总结",
    "投研摘要",
    "投资结论",
}


def body_section(markdown: str) -> str:
    match = re.search(r"## 一、会议纪要(?P<body>.*?)(?=^## 二、参考原文|\Z)", markdown, re.S | re.M)
    return match.group("body") if match else ""


def reference_section(markdown: str) -> str:
    match = re.search(
        r"## 二、参考原文(?P<body>.*?)(?=^## 重要信息修改记录|^## 三、存疑与待确认|\Z)",
        markdown,
        re.S | re.M,
    )
    return match.group("body") if match else ""


def markdown_field(markdown: str, field: str) -> str:
    match = re.search(rf"^\*\*{re.escape(field)}\*\*[:：]\s*(.+?)\s*$", markdown, re.MULTILINE)
    return match.group(1).strip() if match else ""


def metadata_field_present(markdown: str, field: str) -> bool:
    return bool(markdown_field(markdown, field))


def _subheadings(section: str) -> list[str]:
    return re.findall(r"^###(?!#)\s*(.+?)\s*$", section, re.MULTILINE)


def _questions(section: str) -> list[re.Match[str]]:
    return list(QUESTION_LINE_RE.finditer(section))


def _expert_questions_without_answers(section: str) -> list[str]:
    questions = _questions(section)
    missing: list[str] = []
    for index, question in enumerate(questions):
        end = questions[index + 1].start() if index + 1 < len(questions) else len(section)
        lines = [line.strip() for line in section[question.end() : end].splitlines()]
        answer_lines = [
            line
            for line in lines
            if line
            and not line.startswith("#")
            and not re.fullmatch(r"(?:[-*_]\s*){3,}", line)
            and not re.fullmatch(r"\|?\s*(?::?-{3,}:?\s*\|\s*)+(?::?-{3,}:?)?\s*\|?", line)
        ]
        if not answer_lines:
            missing.append(question.group(0).strip())
    return missing


def _validate_meeting_type(markdown: str, meeting_type: str) -> list[str]:
    errors: list[str] = []
    normalized = MEETING_TYPE_ALIASES.get(meeting_type, meeting_type)
    if normalized not in MEETING_TYPES:
        return [f"会议类型只能是: {' / '.join(sorted(MEETING_TYPES | set(MEETING_TYPE_ALIASES)))}"]
    body = body_section(markdown)
    body_headings = _subheadings(body)
    reference_headings = _subheadings(reference_section(markdown))
    if normalized == "多人复盘会":
        if not body_headings:
            errors.append("多人复盘会必须按发言轮次使用三级发言人标题")
        if not reference_headings:
            errors.append("多人复盘会参考原文必须按发言轮次使用三级发言人标题")
        if "标的：" in body:
            errors.append("多人复盘会小段标题不使用 标的： 前缀")
    elif normalized == "公司交流":
        if not metadata_field_present(markdown, "会议标的"):
            errors.append("公司交流必须包含会议元信息字段: 会议标的")
        if not body_headings:
            errors.append("公司交流必须按实际会议环节使用三级标题")
        if not reference_headings:
            errors.append("公司交流参考原文必须按实际会议环节使用三级标题")
    else:
        if body_headings:
            errors.append("专家交流会议纪要不使用三级发言人标题，只保留问题与回答")
        questions = _questions(body)
        if not questions:
            errors.append("专家交流必须使用加粗问题格式，例如 **【问题原文】**")
        missing = _expert_questions_without_answers(body)
        if missing:
            errors.append("专家交流每个问题后必须保留对应回答: " + "；".join(missing[:4]))
        if not reference_headings:
            errors.append("专家交流参考原文必须保留三级发言人标题")
    return errors


def _heading_findings(markdown: str) -> tuple[list[str], list[str]]:
    escapes: list[str] = []
    empty: list[str] = []
    for match in re.finditer(r"(?m)^(#{2,6})\s*(.+?)\s*$", markdown):
        normalized = re.sub(r"\s+", "", match.group(2).strip(" #"))
        if normalized in FORBIDDEN_ESCAPE_HEADINGS:
            escapes.append(match.group(0).strip())
    empty.extend(match.group(0).strip() for match in re.finditer(r"(?m)^#{4,5}\s*【\s*】\s*$", markdown))

    return escapes, empty


def _sector_heading_findings(markdown: str) -> list[str]:
    if markdown_field(markdown, "会议类型") != "多人复盘会":
        return []
    findings: list[str] = []
    lines = [line.strip() for line in body_section(markdown).splitlines()]
    for line in lines:
        if line.startswith("##### "):
            findings.append(line)
            continue
        match = re.fullmatch(r"####\s*【(?P<content>[^】\n]*)】\s*", line)
        if match and "|" in match.group("content"):
            findings.append(line)
    return findings


def _validate_ambiguity_section(markdown: str) -> list[str]:
    if "## 三、存疑与待确认" not in markdown:
        return []
    section = markdown.split("## 三、存疑与待确认", 1)[1]
    table = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    if len(table) < 3:
        return ["存疑与待确认章节存在时必须包含非空 Markdown 表格"]
    headers = tuple(cell.strip() for cell in table[0].strip("|").split("|"))
    if headers not in ALLOWED_AMBIGUITY_HEADERS:
        return ["存疑与待确认表格表头不符合输出合约"]
    original_index = headers.index("原始表述")
    real_rows = []
    for line in table[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if original_index < len(cells) and cells[original_index].lower() not in PLACEHOLDER_VALUES:
            real_rows.append(cells)
    return [] if real_rows else ["存疑与待确认章节存在时必须包含至少一条真实存疑"]


def _validate_correction_section(markdown: str) -> list[str]:
    heading = "## 重要信息修改记录"
    if heading not in markdown:
        return []
    section = markdown.split(heading, 1)[1].split("## 三、存疑与待确认", 1)[0]
    table = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    if len(table) < 3:
        return ["重要信息修改记录存在时必须包含非空 Markdown 表格"]
    headers = tuple(cell.strip() for cell in table[0].strip("|").split("|"))
    if headers != ALLOWED_CORRECTION_HEADERS:
        return ["重要信息修改记录表头必须为: 原始表述 / 校对后 / 依据"]
    real_rows = []
    for line in table[2:]:
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 3 and all(cell.lower() not in PLACEHOLDER_VALUES for cell in cells[:3]):
            real_rows.append(cells)
    return [] if real_rows else ["重要信息修改记录存在时必须包含至少一条真实修改"]


def validate_contract(
    markdown: str,
    *,
    required_terms: list[str] | None = None,
    forbidden_terms: list[str] | None = None,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    stripped = markdown.lstrip()
    if not re.search(r"(?m)^# 投资会议纪要(?:\s|｜|$)", stripped):
        errors.append("一级标题必须以 # 投资会议纪要 开头")
    for field in REQUIRED_METADATA_FIELDS:
        if not metadata_field_present(markdown, field):
            errors.append(f"缺少会议元信息字段: {field}")
    meeting_type = markdown_field(markdown, "会议类型")
    if meeting_type:
        errors.extend(_validate_meeting_type(markdown, meeting_type))

    body_position = markdown.find("## 一、会议纪要")
    reference_position = markdown.find("## 二、参考原文")
    correction_position = markdown.find("## 重要信息修改记录")
    ambiguity_position = markdown.find("## 三、存疑与待确认")
    if body_position < 0 or not body_section(markdown).strip():
        errors.append("缺少或留空必需章节: ## 一、会议纪要")
    if reference_position < 0 or not reference_section(markdown).strip():
        errors.append("缺少或留空必需章节: ## 二、参考原文")
    positions = [
        item
        for item in (body_position, reference_position, correction_position, ambiguity_position)
        if item >= 0
    ]
    if positions != sorted(positions):
        errors.append("必需章节顺序错误")
    for pattern in FORBIDDEN_PATTERNS:
        if re.search(pattern, markdown):
            errors.append(f"包含禁止输出内容: {pattern}")

    escapes, empty = _heading_findings(markdown)
    if escapes:
        errors.append("包含契约外逃逸标题: " + "；".join(escapes[:6]))
    if empty:
        errors.append("标题不得使用空括号占位: " + "；".join(empty[:6]))
    sectors = _sector_heading_findings(markdown)
    if sectors:
        errors.append("多人复盘会标题不得使用五级板块行或 ASCII 分隔符: " + "；".join(sectors[:6]))
    errors.extend(_validate_ambiguity_section(markdown))
    errors.extend(_validate_correction_section(markdown))
    for term in required_terms or []:
        if term not in markdown:
            errors.append(f"缺少样例关键锚点: {term}")
    for term in forbidden_terms or []:
        if term in markdown:
            errors.append(f"包含样例禁止锚点: {term}")
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description="校验会议纪要 Markdown 的客观结构")
    parser.add_argument("markdown_file", help="待校验 Markdown 文件")
    parser.add_argument("--require-term", action="append", default=[], help="开发样例必须保留的文本")
    parser.add_argument("--forbid-term", action="append", default=[], help="开发样例不应出现的文本")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    path = Path(args.markdown_file).expanduser()
    try:
        markdown = path.read_text(encoding="utf-8")
        result = validate_contract(markdown, required_terms=args.require_term, forbidden_terms=args.forbid_term)
    except (OSError, UnicodeDecodeError) as exc:
        result = {"ok": False, "errors": [str(exc)], "warnings": []}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result["ok"]:
            print(f"{path}: ok")
        for warning in result["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        for error in result["errors"]:
            print(f"error: {error}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
