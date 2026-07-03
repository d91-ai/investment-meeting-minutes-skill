#!/usr/bin/env python3
"""Run fixed local regression checks for the meeting-minutes contract."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_CASES_PATH = SKILL_DIR / "references/regression_samples/cases.json"

sys.path.insert(0, str(SCRIPT_DIR))
from validate_meeting_minutes_contract import (  # noqa: E402
    validate_contract,
    validate_timestamp_index_file,
    validate_verification_sidecar,
    validate_word_contract,
)
from export_to_obsidian import convert_markdown_to_docx, export_note  # noqa: E402


def read_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"回归样例格式错误: {path}")
    return cases


def run_case(case: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    file_path = base_dir / str(case["file"])
    if case.get("check") == "text_contains":
        text = file_path.read_text(encoding="utf-8")
        errors: list[str] = []
        warnings: list[str] = []
        for term in [str(term) for term in case.get("required_terms", [])]:
            if term not in text:
                errors.append(f"缺少文本检查锚点: {term}")
        for term in [str(term) for term in case.get("forbidden_terms", [])]:
            if term in text:
                errors.append(f"包含文本检查禁止锚点: {term}")
        result: dict[str, Any] = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
        }
    elif case.get("check") == "export_filename":
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-export-") as tmpdir:
            result_export = export_note(
                file_path,
                Path(tmpdir),
                str(case["meeting_date_override"]) if case.get("meeting_date_override") else None,
            )
            expected_stem = str(case["expected_stem"])
            actual_stems = [result_export.md_path.stem, result_export.docx_path.stem]
            errors = []
            warnings = []
            if actual_stems != [expected_stem, expected_stem]:
                errors.append(f"导出文件名不符合预期: expected={expected_stem} actual={actual_stems}")
            if result_export.md_path.suffix != ".md":
                errors.append(f"Markdown 后缀错误: {result_export.md_path.name}")
            if result_export.docx_path.suffix != ".docx":
                errors.append(f"Word 后缀错误: {result_export.docx_path.name}")
            if not result_export.md_created:
                errors.append(f"Markdown 未生成: {result_export.md_message}")
            if not result_export.docx_created:
                errors.append(f"Word 未生成: {result_export.docx_message}")
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": warnings,
                "md_path": str(result_export.md_path),
                "docx_path": str(result_export.docx_path),
            }
    else:
        markdown = file_path.read_text(encoding="utf-8")
        result = validate_contract(
            markdown,
            required_terms=[str(term) for term in case.get("required_terms", [])],
            forbidden_terms=[str(term) for term in case.get("forbidden_terms", [])],
            source_mode=str(case.get("source_mode") or case.get("mode") or "auto"),
            require_audio_timestamps=bool(case.get("require_audio_timestamps")),
            timestamp_mode=str(case.get("timestamp_mode") or "auto"),
        )
        if case.get("verification_file") or case.get("require_verification"):
            verification_path = base_dir / str(case["verification_file"]) if case.get("verification_file") else None
            verification_result = validate_verification_sidecar(
                verification_path,
                require_verification=bool(case.get("require_verification")),
            )
            result["verification"] = verification_result
            result["errors"].extend(verification_result["errors"])
            result["warnings"].extend(verification_result["warnings"])
            result["ok"] = result["ok"] and verification_result["ok"]
        if case.get("timestamp_index_file"):
            timestamp_index_path = base_dir / str(case["timestamp_index_file"])
            timestamp_index_result = validate_timestamp_index_file(
                timestamp_index_path,
                require_reliable=bool(case.get("timestamp_index_require_reliable")),
            )
            result["timestamp_index"] = timestamp_index_result
            result["errors"].extend(timestamp_index_result["errors"])
            result["warnings"].extend(timestamp_index_result["warnings"])
            result["ok"] = result["ok"] and timestamp_index_result["ok"]
        if case.get("word_export"):
            with tempfile.TemporaryDirectory(prefix="meeting-minutes-regression-") as tmpdir:
                docx_path = Path(tmpdir) / f"{file_path.stem}.docx"
                word_ok, word_message = convert_markdown_to_docx(file_path, docx_path)
                if not word_ok:
                    word_result: dict[str, Any] = {"ok": False, "errors": [word_message], "warnings": []}
                else:
                    word_result = validate_word_contract(docx_path, markdown)
                result["word"] = word_result
                result["errors"].extend(word_result["errors"])
                result["warnings"].extend(word_result["warnings"])
                result["ok"] = result["ok"] and word_result["ok"]
    raw_ok = bool(result["ok"])
    expect_fail = bool(case.get("expect_fail"))
    required_error_terms = [str(term) for term in case.get("required_error_terms", [])]
    required_warning_terms = [str(term) for term in case.get("required_warning_terms", [])]
    error_text = "\n".join(str(error) for error in result.get("errors", []))
    warning_text = "\n".join(str(warning) for warning in result.get("warnings", []))
    expectation_errors: list[str] = []
    if expect_fail:
        if raw_ok:
            expectation_errors.append("负例应失败但实际通过")
        for term in required_error_terms:
            if term not in error_text:
                expectation_errors.append(f"负例缺少预期错误片段: {term}")
        result["ok"] = not expectation_errors
        result["expected_failure"] = raw_ok is False
        result["expectation_errors"] = expectation_errors
    if not expect_fail and required_warning_terms:
        for term in required_warning_terms:
            if term not in warning_text:
                expectation_errors.append(f"样例缺少预期 warning 片段: {term}")
        result["ok"] = bool(result["ok"]) and not expectation_errors
        result["expectation_errors"] = expectation_errors
    result = {
        "name": case.get("name") or file_path.stem,
        "mode": case.get("mode") or "",
        "file": str(file_path),
        **result,
    }
    return result


def print_text(results: list[dict[str, Any]]) -> None:
    for result in results:
        status = "OK" if result["ok"] else "FAIL"
        print(f"[{status}] {result['name']} ({result['mode']})")
        for warning in result["warnings"]:
            print(f"  warning: {warning}")
        expected_failure = bool(result.get("expected_failure"))
        for error in result["errors"]:
            if expected_failure:
                print(f"  expected failure matched: {error}")
            else:
                print(f"  error: {error}")
        for error in result.get("expectation_errors", []):
            print(f"  expectation-error: {error}")


def main() -> int:
    parser = argparse.ArgumentParser(description="运行会议纪要固定回归样例")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH), help="回归样例 cases.json")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    cases_path = Path(args.cases).expanduser()
    base_dir = cases_path.parent
    results = [run_case(case, base_dir) for case in read_cases(cases_path)]
    payload = {
        "ok": all(result["ok"] for result in results),
        "case_count": len(results),
        "results": results,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print_text(results)
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
