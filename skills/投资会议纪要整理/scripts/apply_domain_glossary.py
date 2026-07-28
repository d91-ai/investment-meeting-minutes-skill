#!/usr/bin/env python3
"""确定性领域术语纠错工具。

基于 references/domain_glossary.tsv 的 common_asr_errors 列做反向匹配：
- 误写与标准词是纯同音/近音替换、上下文歧义小 -> action=corrected（高置信，自动纠正）。
- 标准词或误写本身是常用词、歧义大（词库 note 以 [report_only] 标记）-> action=report_only
  （低置信，只报告不纠正，交给既有存疑流程进入 doubtful_items）。

只做术语级替换，绝不动其他文本；任何纠正都记录在 corrections 列表中，不静默覆盖。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_GLOSSARY_PATH = SKILL_DIR / "references" / "domain_glossary.tsv"

REPORT_ONLY_MARKER = "[report_only]"
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_ASCII_WORD_RE = re.compile(r"^[\x21-\x7e]+(?: [\x21-\x7e]+)*$")


@dataclass(frozen=True)
class GlossaryEntry:
    term: str
    category: str
    errors: tuple[str, ...]
    report_only: bool
    note: str = ""


@dataclass(frozen=True)
class Correction:
    original: str
    replacement: str
    term: str
    category: str
    position: int
    line: int
    confidence: str
    action: str  # corrected | report_only

    def as_dict(self) -> dict[str, object]:
        return {
            "original": self.original,
            "replacement": self.replacement,
            "term": self.term,
            "category": self.category,
            "position": self.position,
            "line": self.line,
            "confidence": self.confidence,
            "action": self.action,
        }


def load_glossary(path: Path) -> list[GlossaryEntry]:
    """读取 TSV 词库；列：term / category / common_asr_errors / note。"""
    entries: list[GlossaryEntry] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        header = handle.readline()
        columns = header.rstrip("\n").rstrip("\r").split("\t")
        if columns[:4] != ["term", "category", "common_asr_errors", "note"]:
            raise ValueError(f"词库表头不符合约定: {path}")
        for line_number, raw_line in enumerate(handle, start=2):
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 4:
                raise ValueError(f"词库第 {line_number} 行列数不为 4: {line!r}")
            term, category, errors_raw, note = (part.strip() for part in parts)
            if not term:
                raise ValueError(f"词库第 {line_number} 行 term 为空")
            report_only = note.startswith(REPORT_ONLY_MARKER)
            errors: list[str] = []
            for candidate in errors_raw.split("|"):
                candidate = candidate.strip()
                if not candidate or candidate == term:
                    continue
                if candidate not in errors:
                    errors.append(candidate)
            entries.append(
                GlossaryEntry(
                    term=term,
                    category=category,
                    errors=tuple(errors),
                    report_only=report_only,
                    note=note,
                )
            )
    return entries


def report_only_terms(entries: list[GlossaryEntry]) -> set[str]:
    return {entry.term for entry in entries if entry.report_only}


def build_asr_hotword_map(entries: list[GlossaryEntry]) -> dict[str, str]:
    """构造 ASR 后处理显式映射（误写 -> 标准词）。

    只收录非 report_only 的术语；report_only 术语歧义大，交给校对层处理，
    不在 ASR 阶段做确定性替换。identity 映射（误写 == 标准词）已在 load_glossary 过滤。
    """
    mapping: dict[str, str] = {}
    for entry in entries:
        if entry.report_only:
            continue
        for error in entry.errors:
            mapping.setdefault(error, entry.term)
    return mapping


def _iter_error_spans(text: str, error: str) -> list[tuple[int, int]]:
    """返回 error 在 text 中的全部 (start, end) 区间。

    纯 ASCII 误写（如 ``Saas``、``P E``）使用字母数字边界匹配，避免误伤
    更长英文单词；含 CJK 的误写使用普通子串匹配。
    """
    if _ASCII_WORD_RE.match(error) and not _CJK_RE.search(error):
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(error) + r"(?![A-Za-z0-9])")
        return [(match.start(), match.end()) for match in pattern.finditer(text)]
    spans: list[tuple[int, int]] = []
    start = 0
    while True:
        index = text.find(error, start)
        if index < 0:
            break
        spans.append((index, index + len(error)))
        start = index + len(error)
    return spans


def find_corrections(text: str, entries: list[GlossaryEntry]) -> list[Correction]:
    """在原文中定位全部误写命中；区间不重叠，长误写优先。"""
    candidates: list[tuple[int, int, GlossaryEntry, str]] = []
    for entry in entries:
        for error in entry.errors:
            for start, end in _iter_error_spans(text, error):
                candidates.append((start, end, entry, error))
    # 位置升序、同位置长匹配优先
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, GlossaryEntry, str]] = []
    occupied: list[tuple[int, int]] = []
    for start, end, entry, error in candidates:
        if any(not (end <= taken_start or start >= taken_end) for taken_start, taken_end in occupied):
            continue
        selected.append((start, end, entry, error))
        occupied.append((start, end))
    corrections: list[Correction] = []
    for start, end, entry, error in selected:
        corrections.append(
            Correction(
                original=error,
                replacement=entry.term,
                term=entry.term,
                category=entry.category,
                position=start,
                line=text.count("\n", 0, start) + 1,
                confidence="low" if entry.report_only else "high",
                action="report_only" if entry.report_only else "corrected",
            )
        )
    corrections.sort(key=lambda item: item.position)
    return corrections


def apply_corrections(text: str, corrections: list[Correction]) -> str:
    """只应用 action=corrected 的替换；report_only 不改写原文。"""
    updated = text
    for correction in sorted(
        (item for item in corrections if item.action == "corrected"),
        key=lambda item: item.position,
        reverse=True,
    ):
        start = correction.position
        end = start + len(correction.original)
        if updated[start:end] != correction.original:
            raise RuntimeError(f"纠正区间与原文不一致: {correction.original!r}@{start}")
        updated = updated[:start] + correction.replacement + updated[end:]
    return updated


def correct_text(text: str, entries: list[GlossaryEntry]) -> tuple[str, list[Correction]]:
    corrections = find_corrections(text, entries)
    return apply_corrections(text, corrections), corrections


def build_report(
    *,
    input_path: Path,
    glossary_path: Path,
    entries: list[GlossaryEntry],
    corrections: list[Correction],
    output_path: Path | None,
) -> dict[str, object]:
    corrected = [item for item in corrections if item.action == "corrected"]
    reported = [item for item in corrections if item.action == "report_only"]
    return {
        "input": str(input_path),
        "glossary": str(glossary_path),
        "output": str(output_path) if output_path else "",
        "stats": {
            "glossary_terms": len(entries),
            "report_only_terms": len(report_only_terms(entries)),
            "error_patterns": sum(len(entry.errors) for entry in entries),
            "matches": len(corrections),
            "corrected": len(corrected),
            "report_only": len(reported),
        },
        "corrections": [item.as_dict() for item in corrections],
    }


def print_text_report(report: dict[str, object]) -> None:
    stats = report["stats"]
    print(f"输入: {report['input']}")
    print(f"词库: {report['glossary']}（{stats['glossary_terms']} 词 / {stats['error_patterns']} 误写模式）")
    print(f"命中 {stats['matches']} 处：自动纠正 {stats['corrected']} 处，只报告 {stats['report_only']} 处")
    for item in report["corrections"]:
        marker = "纠正" if item["action"] == "corrected" else "存疑"
        print(
            f"  [{marker}] 行{item['line']} 列{item['position']}: "
            f"{item['original']} -> {item['replacement']}（{item['category']}，置信度 {item['confidence']}）"
        )
    if report.get("output"):
        print(f"已写出: {report['output']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="投资会议领域术语确定性纠错（基于 domain_glossary.tsv）")
    parser.add_argument("input_file", help="转写/纪要文本文件")
    parser.add_argument(
        "--glossary",
        default=str(DEFAULT_GLOSSARY_PATH),
        help="词库 TSV 路径，默认 references/domain_glossary.tsv",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出 corrections 与统计")
    parser.add_argument("--check", action="store_true", help="只报告不写文件（默认行为）")
    parser.add_argument("--apply", action="store_true", help="写出纠正后文件（默认 <stem>.glossary_corrected.txt）")
    parser.add_argument("--output", default="", help="--apply 时的输出路径")
    parser.add_argument("--in-place", action="store_true", help="--apply 时直接写回原文件（需显式指定）")
    args = parser.parse_args()

    input_path = Path(args.input_file).expanduser().resolve()
    if not input_path.is_file():
        print(f"输入文件不存在: {input_path}", file=sys.stderr)
        return 1
    glossary_path = Path(args.glossary).expanduser().resolve()
    if not glossary_path.is_file():
        print(f"词库文件不存在: {glossary_path}", file=sys.stderr)
        return 1
    if args.in_place and not args.apply:
        print("--in-place 必须与 --apply 一起使用", file=sys.stderr)
        return 2
    if args.in_place and args.output:
        print("--in-place 与 --output 不能同时使用", file=sys.stderr)
        return 2

    try:
        entries = load_glossary(glossary_path)
    except ValueError as exc:
        print(f"词库解析失败: {exc}", file=sys.stderr)
        return 1

    text = input_path.read_text(encoding="utf-8")
    corrected_text, corrections = correct_text(text, entries)

    output_path: Path | None = None
    if args.apply:
        if args.in_place:
            output_path = input_path
        elif args.output:
            output_path = Path(args.output).expanduser().resolve()
        else:
            output_path = input_path.with_name(f"{input_path.stem}.glossary_corrected{input_path.suffix}")
        output_path.write_text(corrected_text, encoding="utf-8", newline="")

    report = build_report(
        input_path=input_path,
        glossary_path=glossary_path,
        entries=entries,
        corrections=corrections,
        output_path=output_path,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_text_report(report)
        if not args.apply:
            print("（--check 模式：未写出文件；使用 --apply 生成纠正后文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
