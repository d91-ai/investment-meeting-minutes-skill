#!/usr/bin/env python3
"""Run focused development regressions for user-visible meeting-minutes behavior."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_CASES_PATH = SKILL_DIR / "references/regression_samples/cases.json"

from assemble_speaker_turn_edits import assemble_returns  # noqa: E402
from build_speaker_turn_manifest import build_manifest  # noqa: E402
from export_to_obsidian import detect_filename_title, normalize_meeting_date  # noqa: E402
from validate_meeting_minutes_contract import (  # noqa: E402
    validate_contract,
    validate_timestamp_index_file,
    validate_verification_sidecar,
)


def read_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"回归样例格式错误: {path}")
    return cases


def _turn_source(lengths: list[int]) -> str:
    blocks = []
    for index, length in enumerate(lengths, start=1):
        blocks.append(f"说话人 {index}\n" + (chr(0x4E00 + index) * length))
    return "\n".join(blocks)


def check_partition(scenario: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if scenario == "direct_12000":
        manifest = build_manifest("甲" * 12_000)
        if manifest["routing"]["mode"] != "direct" or manifest["packages"]:
            errors.append("12,000 字材料必须 direct 且不生成 package")
    elif scenario == "sharded_12001":
        manifest = build_manifest("甲" * 12_001)
        if manifest["routing"]["mode"] != "sharded" or not manifest["packages"]:
            errors.append("12,001 字材料必须进入 sharded")
    elif scenario == "total_unbounded":
        manifest = build_manifest(_turn_source([17_000, 17_000, 17_000]))
        if manifest["routing"]["mode"] != "sharded":
            errors.append("长材料必须进入 sharded")
        if any(package["char_count"] > 16_000 for package in manifest["packages"]):
            errors.append("单包不得超过 16,000 字")
        if manifest["source_char_count"] <= 16_000 or not manifest["routing"]["total_length_is_unbounded"]:
            errors.append("总材料长度不得受 16,000 字限制")
    elif scenario == "qa_atomic":
        source = _turn_source([5_000, 7_000, 2_000])
        initial = build_manifest(source)
        turn_ids = [turn["turn_id"] for turn in initial["turns"]]
        adjusted = build_manifest(source, package_breaks=[turn_ids[0], turn_ids[1]])
        second_package = adjusted["packages"][1]["turn_ids"]
        if second_package != turn_ids[1:]:
            errors.append("模型调整边界后，问题与回答必须保持在同一包")
    elif scenario == "oversized":
        manifest = build_manifest("甲" * 33_000)
        if len(manifest["turns"]) < 3:
            errors.append("超过单包上限的连续发言必须安全拆分")
        if any(turn["char_count"] > 16_000 for turn in manifest["turns"]):
            errors.append("拆分后的 turn 不得超过 16,000 字")
    elif scenario == "coverage":
        manifest = build_manifest(_turn_source([4_000] * 5))
        expected = [turn["turn_id"] for turn in manifest["turns"]]
        actual = [turn_id for package in manifest["packages"] for turn_id in package["turn_ids"]]
        if actual != expected or len(actual) != len(set(actual)):
            errors.append("package 必须按来源顺序不重不漏覆盖所有 turn")
    elif scenario == "timestamped_labels":
        source = "[01:35] 发言人1: 第一段\n[01:42:08] 发言人2：第二段"
        manifest = build_manifest(source)
        speakers = [turn["speaker_label"] for turn in manifest["turns"]]
        texts = [turn["text"] for turn in manifest["turns"]]
        if speakers != ["发言人1", "发言人2"] or texts != ["第一段", "第二段"]:
            errors.append("行首时间戳后的显式发言人标签必须被识别并从正文 span 中移除")
    else:
        errors.append(f"未知 partition scenario: {scenario}")
    return errors, warnings


def check_assembly() -> tuple[list[str], list[str]]:
    manifest = build_manifest(_turn_source([7_000, 7_000, 7_000]))
    returns = []
    for package in manifest["packages"]:
        returns.append(
            {
                "package_id": package["package_id"],
                "turns": [
                    {
                        "turn_id": turn_id,
                        "reference_text": f"参考-{turn_id}",
                        "minutes_text": f"纪要-{turn_id}",
                    }
                    for turn_id in package["turn_ids"]
                ],
            }
        )
    assembled = assemble_returns(manifest, returns)
    expected = [turn["turn_id"] for turn in manifest["turns"]]
    actual = [turn["turn_id"] for turn in assembled["turns"]]
    errors = [] if assembled["coverage"]["complete"] and actual == expected else ["长材料组装未完整保持来源顺序"]
    return errors, []


def run_case(case: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    case_id = str(case.get("id") or "unnamed")
    check = str(case.get("check") or "markdown")
    errors: list[str] = []
    warnings: list[str] = []
    try:
        if check == "text_contains":
            text = (base_dir / str(case["file"])).read_text(encoding="utf-8")
            for term in case.get("required_terms", []):
                if str(term) not in text:
                    errors.append(f"缺少文档锚点: {term}")
            for term in case.get("forbidden_terms", []):
                if str(term) in text:
                    errors.append(f"包含禁止文档锚点: {term}")
        elif check == "markdown":
            path = base_dir / str(case["file"])
            markdown = path.read_text(encoding="utf-8")
            validation = validate_contract(
                markdown,
                required_terms=[str(item) for item in case.get("required_terms", [])],
                forbidden_terms=[str(item) for item in case.get("forbidden_terms", [])],
                source_mode=case.get("source_mode"),
                timestamp_mode=case.get("timestamp_mode", "auto"),
            )
            errors.extend(str(item) for item in validation.get("errors", []))
            warnings.extend(str(item) for item in validation.get("warnings", []))
            verification = case.get("verification_file")
            if verification or case.get("require_verification"):
                result = validate_verification_sidecar(
                    base_dir / str(verification) if verification else None,
                    require_verification=bool(case.get("require_verification")),
                )
                errors.extend(str(item) for item in result.get("errors", []))
                warnings.extend(str(item) for item in result.get("warnings", []))
            timestamp = case.get("timestamp_index_file")
            if timestamp:
                result = validate_timestamp_index_file(
                    base_dir / str(timestamp),
                    require_reliable=bool(case.get("timestamp_index_require_reliable")),
                )
                errors.extend(str(item) for item in result.get("errors", []))
                warnings.extend(str(item) for item in result.get("warnings", []))
        elif check == "timestamp_index":
            result = validate_timestamp_index_file(
                base_dir / str(case["file"]),
                require_reliable=bool(case.get("require_reliable")),
            )
            errors.extend(str(item) for item in result.get("errors", []))
            warnings.extend(str(item) for item in result.get("warnings", []))
        elif check == "export_filename":
            path = base_dir / str(case["file"])
            markdown = path.read_text(encoding="utf-8")
            date = normalize_meeting_date(case.get("meeting_date_override"), markdown)
            stem = f"{date} - {detect_filename_title(markdown, path.name)}"
            if expected := case.get("expected_stem"):
                if stem != expected:
                    errors.append(f"导出文件名不匹配: {stem} != {expected}")
        elif check == "partition":
            errors, warnings = check_partition(str(case.get("scenario") or ""))
        elif check == "assembly":
            errors, warnings = check_assembly()
        else:
            errors.append(f"未知 check: {check}")
    except Exception as exc:
        errors.append(f"{exc.__class__.__name__}: {exc}")

    joined_errors = "\n".join(errors)
    joined_warnings = "\n".join(warnings)
    expected_fail = bool(case.get("expect_fail"))
    if expected_fail:
        passed = bool(errors)
    else:
        passed = not errors
    for term in case.get("required_error_terms", []):
        if str(term) not in joined_errors:
            passed = False
    for term in case.get("required_warning_terms", []):
        if str(term) not in joined_warnings:
            passed = False

    return {
        "id": case_id,
        "check": check,
        "passed": passed,
        "expected_fail": expected_fail,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行会议纪要轻量开发回归")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    cases_path = Path(args.cases).expanduser()
    cases = read_cases(cases_path)
    results = [run_case(case, cases_path.parent) for case in cases]
    failed = [result for result in results if not result["passed"]]
    payload = {
        "ok": not failed,
        "case_count": len(results),
        "passed_count": len(results) - len(failed),
        "failed_count": len(failed),
        "results": results,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(("PASS" if result["passed"] else "FAIL") + f" {result['id']}")
        print(f"{payload['passed_count']}/{payload['case_count']} passed")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
