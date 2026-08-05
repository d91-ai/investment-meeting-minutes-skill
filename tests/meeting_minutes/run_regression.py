#!/usr/bin/env python3
"""Run focused development regressions outside the installed Skill."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
SKILL_SCRIPTS = REPO_ROOT / "skills/投资会议纪要整理/scripts"
LOCAL_TOOLS = REPO_ROOT / "tools/meeting_minutes"
DEFAULT_CASES_PATH = SCRIPT_DIR / "fixtures/cases.json"
sys.path.insert(0, str(SKILL_SCRIPTS))
sys.path.insert(0, str(LOCAL_TOOLS))

from assemble_speaker_turn_edits import assemble_returns  # noqa: E402
from build_speaker_turn_manifest import build_manifest  # noqa: E402
from export_to_obsidian import detect_filename_title, normalize_meeting_date  # noqa: E402
from query_symbol_candidates import query_symbols  # noqa: E402
from transcribe_audio import _audio_segment_to_wav, _generate_sensevoice_segment_results  # noqa: E402
from validate_meeting_minutes_contract import validate_contract  # noqa: E402


def read_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"回归样例格式错误: {path}")
    return cases


def _turn_source(lengths: list[int]) -> str:
    return "\n".join(
        f"说话人 {index}\n" + chr(0x4E00 + index) * length
        for index, length in enumerate(lengths, start=1)
    )


def check_partition(scenario: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    if scenario == "linear_single":
        manifest = build_manifest("甲" * 12_000)
        if len(manifest["packages"]) != 1:
            errors.append("单包容量的材料应只产生一个 package")
    elif scenario == "linear_multiple":
        manifest = build_manifest(_turn_source([5_000, 5_000, 5_000]))
        sizes = [int(item["char_count"]) for item in manifest["packages"]]
        if len(sizes) != 2 or any(size > 12_000 for size in sizes):
            errors.append(f"线性容量分包结果错误: {sizes}")
    elif scenario == "explicit_breaks":
        source = _turn_source([5_000, 7_000, 2_000])
        initial = build_manifest(source)
        turn_ids = [str(turn["turn_id"]) for turn in initial["turns"]]
        adjusted = build_manifest(source, package_breaks=[turn_ids[0], turn_ids[1]])
        if adjusted["packages"][1]["turn_ids"] != turn_ids[1:]:
            errors.append("显式语义边界必须按来源顺序生效")
    elif scenario == "oversized":
        manifest = build_manifest("甲" * 33_000)
        if len(manifest["turns"]) < 3 or any(int(turn["char_count"]) > 16_000 for turn in manifest["turns"]):
            errors.append("超过 hard limit 的连续发言必须按段落或句子拆分")
    elif scenario == "coverage":
        manifest = build_manifest(_turn_source([4_000] * 5))
        expected = [str(turn["turn_id"]) for turn in manifest["turns"]]
        actual = [turn_id for package in manifest["packages"] for turn_id in package["turn_ids"]]
        if actual != expected or len(actual) != len(set(actual)):
            errors.append("package 必须按来源顺序不重不漏覆盖所有 turn")
    elif scenario == "timestamped_labels":
        manifest = build_manifest("[01:35] 发言人1: 第一段\n[01:42:08] 发言人2：第二段")
        speakers = [turn["speaker_label"] for turn in manifest["turns"]]
        texts = [turn["text"] for turn in manifest["turns"]]
        if speakers != ["发言人1", "发言人2"] or texts != ["第一段", "第二段"]:
            errors.append("行首时间戳后的显式发言人标签必须被识别")
    elif scenario == "linear_scale":
        source = _turn_source([200] * 2_000)
        started = time.perf_counter()
        manifest = build_manifest(source)
        elapsed = time.perf_counter() - started
        if elapsed > 2 or not manifest["packages"]:
            errors.append(f"2,000 turns 线性分包过慢: {elapsed:.3f}s")
    else:
        errors.append(f"未知 partition scenario: {scenario}")
    return errors, []


def check_assembly() -> tuple[list[str], list[str]]:
    manifest = build_manifest(_turn_source([7_000, 7_000, 7_000]))
    returns: list[dict[str, Any]] = []
    omitted_turn_id = str(manifest["turns"][1]["turn_id"])
    for package in manifest["packages"]:
        payload_turns: list[dict[str, Any]] = []
        for turn_id in package["turn_ids"]:
            if turn_id == omitted_turn_id:
                payload_turns.append(
                    {
                        "turn_id": turn_id,
                        "reference_segments": [],
                        "reference_omission_reason": "整轮为无信息流程用语",
                        "minutes_segments": [],
                        "minutes_omission_reason": "整轮无实质信息",
                    }
                )
            else:
                payload_turns.append(
                    {
                        "turn_id": turn_id,
                        "reference_segments": [{"speaker_label": "专家", "text": f"参考-{turn_id}"}],
                        "minutes_segments": [{"kind": "paragraph", "speaker_label": "专家", "text": f"纪要-{turn_id}"}],
                    }
                )
        returns.append({"package_id": package["package_id"], "turns": payload_turns})

    assembled = assemble_returns(manifest, returns)
    expected = [str(turn["turn_id"]) for turn in manifest["turns"]]
    actual = [str(turn["turn_id"]) for turn in assembled["turns"]]
    errors: list[str] = []
    if assembled["schema_version"] != "1.0" or actual != expected:
        errors.append("当前 package 格式组装必须完整保持来源顺序")

    broken = json.loads(json.dumps(returns, ensure_ascii=False))
    broken[0]["turns"][0].pop("reference_segments", None)
    try:
        assemble_returns(manifest, broken)
    except ValueError as exc:
        if "reference_segments" not in str(exc):
            errors.append("旧 package 格式拒绝信息不明确")
    else:
        errors.append("缺少当前 reference_segments 时必须拒绝组装")
    return errors, []


def check_symbol_guidance() -> tuple[list[str], list[str]]:
    errors: list[str] = []
    candidate = query_symbols(
        "测试公司",
        market="all",
        limit=8,
        root=Path("."),
        alias_path=Path("aliases.csv"),
        rows=[{"symbol": "000001.SZ", "code": "000001", "name": "测试公司", "market": "A", "source": "synthetic"}],
        aliases=[],
    )
    not_found = query_symbols(
        "不存在公司",
        market="all",
        limit=8,
        root=Path("."),
        alias_path=Path("aliases.csv"),
        rows=[],
        aliases=[],
    )
    if "不得仅因本地结果非唯一直接列入存疑" not in str(candidate.get("recommendation")):
        errors.append("candidate_only JSON 必须提示先查上下文和定向外部核验")
    if "公开身份问题再做定向外部查询" not in str(not_found.get("recommendation")) or "仍不唯一时才列入存疑" not in str(not_found.get("recommendation")):
        errors.append("not_found JSON 必须提示定向外部查询后仍不唯一才列存疑")
    return errors, []


def check_asr_helper(scenario: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    if scenario == "pcm_slice":
        with tempfile.TemporaryDirectory(prefix="meeting-asr-regression-") as tmp:
            source = Path(tmp) / "normalized.wav"
            output = Path(tmp) / "segment.wav"
            with wave.open(str(source), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16_000)
                audio.writeframes(b"\x01\x00" * 32_000)
            _audio_segment_to_wav(source, output, 250, 1_250)
            with wave.open(str(output), "rb") as segment:
                if segment.getnchannels() != 1 or segment.getframerate() != 16_000 or segment.getnframes() != 16_000:
                    errors.append("PCM 分段必须保持 mono/16k 和正确时长")
    elif scenario == "segment_batch":
        class FakeBatchModel:
            def __init__(self) -> None:
                self.calls: list[Any] = []

            def generate(self, *, input: Any, **_: Any) -> list[dict[str, str]]:
                self.calls.append(input)
                if not isinstance(input, list):
                    raise AssertionError("expected batch")
                return [{"text": Path(item).stem} for item in input]

        model = FakeBatchModel()
        paths = [Path(f"segment_{index:05d}.wav") for index in range(8)]
        results, mode = _generate_sensevoice_segment_results(model, paths, "zh")
        if mode != "dynamic_batch" or len(model.calls) != 1 or len(results) != len(paths):
            errors.append("SenseVoice segments 必须优先使用一次 dynamic batch")
    elif scenario == "segment_failure_terminal":
        class AlwaysFailModel:
            def generate(self, *, input: Any, **_: Any) -> list[dict[str, str]]:
                raise RuntimeError(f"segment inference failed: {input}")

        try:
            _generate_sensevoice_segment_results(AlwaysFailModel(), [Path("segment_00000.wav")], "zh")
        except RuntimeError as exc:
            if "batch and sequential segment inference both failed" not in str(exc):
                errors.append("segment 失败应保留 batch 与 sequential 证据")
        else:
            errors.append("batch 与 sequential 都失败时不得静默降级")
    else:
        errors.append(f"未知 ASR helper scenario: {scenario}")
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
            markdown = (base_dir / str(case["file"])).read_text(encoding="utf-8")
            result = validate_contract(
                markdown,
                required_terms=[str(item) for item in case.get("required_terms", [])],
                forbidden_terms=[str(item) for item in case.get("forbidden_terms", [])],
            )
            errors.extend(str(item) for item in result["errors"])
            warnings.extend(str(item) for item in result["warnings"])
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
        elif check == "symbol_guidance":
            errors, warnings = check_symbol_guidance()
        elif check == "asr_performance_helper":
            errors, warnings = check_asr_helper(str(case.get("scenario") or ""))
        else:
            errors.append(f"未知 check: {check}")
    except Exception as exc:
        errors.append(f"{exc.__class__.__name__}: {exc}")

    expected_fail = bool(case.get("expect_fail"))
    passed = bool(errors) if expected_fail else not errors
    joined_errors = "\n".join(errors)
    joined_warnings = "\n".join(warnings)
    for term in case.get("required_error_terms", []):
        if str(term) not in joined_errors:
            passed = False
    for term in case.get("required_warning_terms", []):
        if str(term) not in joined_warnings:
            passed = False
    return {"id": case_id, "check": check, "passed": passed, "expected_fail": expected_fail, "errors": errors, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description="运行会议纪要轻量开发回归")
    parser.add_argument("--cases", default=str(DEFAULT_CASES_PATH))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    cases_path = Path(args.cases).expanduser()
    results = [run_case(case, cases_path.parent) for case in read_cases(cases_path)]
    failed = [result for result in results if not result["passed"]]
    payload = {"ok": not failed, "case_count": len(results), "passed_count": len(results) - len(failed), "failed_count": len(failed), "results": results}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for result in results:
            print(("PASS" if result["passed"] else "FAIL") + f" {result['id']}")
        print(f"{payload['passed_count']}/{payload['case_count']} passed")
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
