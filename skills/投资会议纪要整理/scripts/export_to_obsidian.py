#!/usr/bin/env python3
"""
Export a finalized meeting note to the user's Obsidian workflow as Markdown.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

DEFAULT_WORKSPACE_ROOT = (
    Path(os.environ["INVESTMENT_MINUTES_WORKSPACE"]).expanduser()
    if os.environ.get("INVESTMENT_MINUTES_WORKSPACE")
    else Path.home() / "Documents/会议纪要整理"
)
DEFAULT_EXPORT_DIR = DEFAULT_WORKSPACE_ROOT / "01 Projects/会议纪要"
INVALID_FILENAME_CHARS = r'[\\/:*?"<>|]+'
CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
KNOWN_REVIEW_SERIES = (
    "东方路",
    "程郡",
    "舵主",
    "科技",
    "华鑫周会",
    "电子",
    "苏总",
    "纪博",
    "崔磊",
    "李旦",
    "易欢欢",
)
MEETING_TYPE_ALIASES = {"上市公司交流": "公司交流"}
FILENAME_PLACEHOLDERS = {"", "会议系列", "会议类型", "未命名会议", "待确认"}
ALLOWED_PREEXPORT_ACTIONS = {
    ("collect_or_dispatch_phase_artifacts", "final_verification"),
    ("continue_without_user_intervention", "complete"),
}


def validate_utf8_text_file(path: Path, *, require_cjk: bool = False) -> tuple[bool, str]:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        return False, f"{path}: 不是有效 UTF-8: {exc}"
    if "\ufffd" in text:
        return False, f"{path}: 检测到 Unicode 替换字符 U+FFFD，疑似编码损坏"
    if require_cjk and not CJK_PATTERN.search(text):
        return False, f"{path}: 未检测到中文字符"
    return True, "ok"


@dataclass
class ExportResult:
    md_path: Path
    md_created: bool
    md_message: str


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def artifact_value(path: Path, artifact_type: str) -> dict[str, object] | None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    if payload.get("artifact_type") == artifact_type and isinstance(payload.get("artifact"), dict):
        return payload["artifact"]
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict) and isinstance(artifacts.get(artifact_type), dict):
        return artifacts[artifact_type]
    return None


def resolve_artifact_path(raw_path: str, summary: dict[str, object], summary_path: Path) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    task_dir = Path(str(summary.get("task_dir") or summary_path.parent)).expanduser()
    return (task_dir / path).resolve()


def validate_mas_draft_gate(summary_path: Path, source_file: Path) -> dict[str, object]:
    errors: list[str] = []
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"MAS run summary 无法读取或解析: {exc}"]}
    if not isinstance(summary, dict):
        return {"ok": False, "errors": ["MAS run summary 顶层必须是 JSON object"]}
    if summary.get("ok") is not True:
        errors.append("MAS collector 顶层 ok 不是 true")

    draft_gate = next(
        (
            gate
            for gate in summary.get("phase_gates", [])
            if isinstance(gate, dict) and gate.get("phase") == "draft_review"
        ),
        None,
    )
    if not isinstance(draft_gate, dict) or draft_gate.get("status") != "complete":
        errors.append("MAS draft_review phase gate 尚未完成")

    next_action = summary.get("next_action")
    action_key = (
        str(next_action.get("type") or ""),
        str(next_action.get("phase") or ""),
    ) if isinstance(next_action, dict) else ("", "")
    if action_key not in ALLOWED_PREEXPORT_ACTIONS:
        errors.append(f"MAS 仍有待处理动作，不能导出: {action_key[0] or 'missing'} / {action_key[1] or 'missing'}")

    artifact_sources = summary.get("artifact_sources")
    source_by_type = {
        str(item.get("artifact_type") or ""): str(item.get("path") or "")
        for item in artifact_sources or []
        if isinstance(item, dict)
    }
    required_reviews = ["fidelity_review"]
    if "target_attribution_review" in set(str(item) for item in summary.get("artifact_types", [])):
        required_reviews.append("target_attribution_review")

    source_resolved = source_file.resolve()
    source_hash = file_sha256(source_resolved)
    for review_type in required_reviews:
        raw_path = source_by_type.get(review_type, "")
        if not raw_path:
            errors.append(f"MAS summary 缺少 {review_type} artifact 来源")
            continue
        review_path = resolve_artifact_path(raw_path, summary, summary_path)
        try:
            review = artifact_value(review_path, review_type)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            errors.append(f"无法读取 {review_type} artifact: {exc}")
            continue
        if not isinstance(review, dict):
            errors.append(f"{review_type} artifact 结构无效")
            continue
        reviewed_path = Path(str(review.get("reviewed_markdown_path") or "")).expanduser()
        if not reviewed_path.is_absolute():
            reviewed_path = (review_path.parent / reviewed_path).resolve()
        else:
            reviewed_path = reviewed_path.resolve()
        if reviewed_path != source_resolved:
            errors.append(f"{review_type} 未绑定当前待导出 Markdown 路径")
        if str(review.get("reviewed_markdown_sha256") or "").lower() != source_hash:
            errors.append(f"{review_type} 未绑定当前待导出 Markdown SHA-256")
    return {"ok": not errors, "errors": errors}


def run_checked(command: list[str], label: str, *, json_output: bool = False) -> dict[str, object]:
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit={completed.returncode}"
        raise ValueError(f"{label}失败: {detail}")
    if not json_output:
        return {"ok": True}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}未返回有效 JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise ValueError(f"{label}未通过: {payload}")
    return payload


def run_export_preflight(
    source_file: Path,
    mas_summary: Path,
    *,
    verification: Path | None = None,
    require_verification: bool = False,
    timestamp_index: Path | None = None,
    require_reliable_timestamp_index: bool = False,
    source_mode: str = "auto",
    timestamp_mode: str = "auto",
    require_audio_timestamps: bool = False,
) -> None:
    run_checked(
        [sys.executable, str(SCRIPT_DIR / "validate_utf8_text.py"), str(source_file), "--require-cjk"],
        "UTF-8 校验",
    )
    contract_command = [
        sys.executable,
        str(SCRIPT_DIR / "validate_meeting_minutes_contract.py"),
        str(source_file),
        "--source-mode",
        source_mode,
        "--timestamp-mode",
        timestamp_mode,
        "--json",
    ]
    if verification:
        contract_command.extend(["--verification", str(verification)])
    if require_verification:
        contract_command.append("--require-verification")
    if timestamp_index:
        contract_command.extend(["--timestamp-index", str(timestamp_index)])
    if require_reliable_timestamp_index:
        contract_command.append("--require-reliable-timestamp-index")
    if require_audio_timestamps:
        contract_command.append("--require-audio-timestamps")
    run_checked(contract_command, "Markdown 主契约校验", json_output=True)
    run_checked(
        [sys.executable, str(SCRIPT_DIR / "run_meeting_minutes_regression.py"), "--json"],
        "固定回归",
        json_output=True,
    )
    mas_result = validate_mas_draft_gate(mas_summary, source_file)
    if mas_result.get("ok") is not True:
        raise ValueError("MAS 草稿审核门禁失败: " + "；".join(str(item) for item in mas_result.get("errors", [])))


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(INVALID_FILENAME_CHARS, "-", name).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "未命名会议"


def markdown_field(markdown: str, field: str, fallback: str = "") -> str:
    pattern = re.compile(rf"^{re.escape(field)}[:：]\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(markdown)
    return match.group(1).strip() if match else fallback


def strip_suffix(value: str, suffixes: tuple[str, ...]) -> str:
    cleaned = value.strip()
    for suffix in sorted(suffixes, key=len, reverse=True):
        if cleaned.endswith(suffix):
            return cleaned[: -len(suffix)].strip(" -_—–｜|")
    return cleaned


def infer_review_series(content: str, source_name: str) -> str:
    explicit = markdown_field(content, "会议系列", "").strip()
    if explicit not in FILENAME_PLACEHOLDERS:
        return sanitize_filename(explicit)

    meeting_title = markdown_field(content, "会议标题", "").strip()
    haystacks = (meeting_title, source_name)
    matches = [series for series in KNOWN_REVIEW_SERIES if any(series in value for value in haystacks)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"多人复盘会匹配到多个会议系列: {', '.join(matches)}；请向用户确认")
    raise ValueError("无法确定多人复盘会的会议系列；请从原始文件名匹配或向用户确认")


def detect_filename_title(content: str, source_name: str) -> str:
    meeting_type_raw = markdown_field(content, "会议类型", "").strip()
    meeting_type = MEETING_TYPE_ALIASES.get(meeting_type_raw, meeting_type_raw)
    meeting_title = markdown_field(content, "会议标题", "").strip()

    if meeting_type == "多人复盘会":
        return infer_review_series(content, source_name)
    if meeting_type == "公司交流":
        company_name = strip_suffix(meeting_title, ("上市公司交流会议", "上市公司交流", "交流会议", "交流"))
        if not company_name or company_name in FILENAME_PLACEHOLDERS:
            raise ValueError("无法从会议标题确定公司名；请向用户确认")
        return sanitize_filename(f"{company_name} - 上市公司交流")
    if meeting_type == "专家交流":
        topic = strip_suffix(meeting_title, ("专家交流会议", "专家交流"))
        if not topic or topic in FILENAME_PLACEHOLDERS:
            raise ValueError("无法从会议标题确定专家交流主题；请向用户确认")
        return sanitize_filename(f"{topic} - 专家交流")
    raise ValueError("会议类型必须是多人复盘会、公司交流或专家交流")


def parse_meeting_date(raw: str, label: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ValueError(f"{label} 必须为 YYYY-MM-DD 且为合法日期: {raw}")
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{label} 必须为 YYYY-MM-DD 且为合法日期: {raw}") from exc


def normalize_meeting_date(date_override: str | None, content: str = "") -> str:
    override = (date_override or "").strip()
    if override:
        return parse_meeting_date(override, "--meeting-date")
    metadata_date = markdown_field(content, "会议日期", "").strip()
    if metadata_date:
        return parse_meeting_date(metadata_date, "会议日期")
    return datetime.now().strftime("%Y-%m-%d")


def output_path_candidates(export_dir: Path, filename_base: str) -> list[Path]:
    candidates = [export_dir / f"{filename_base}.md"]
    stamp = datetime.now().strftime("%H%M%S")
    for idx in range(1, 1000):
        suffix = f"-{stamp}" if idx == 1 else f"-{stamp}-{idx}"
        candidates.append(export_dir / f"{filename_base}{suffix}.md")
    return candidates


def publish_completed_file(part_path: Path, export_dir: Path, filename_base: str) -> Path:
    """Atomically publish a completed file without exposing partial Markdown."""
    for candidate in output_path_candidates(export_dir, filename_base):
        try:
            os.link(part_path, candidate)
        except FileExistsError:
            continue
        except OSError as exc:
            raise OSError(
                exc.errno,
                "目标文件系统不支持安全的原子无覆盖发布；未采用可能覆盖或暴露半文件的降级方案",
                str(candidate),
            ) from exc
        return candidate
    raise FileExistsError(f"无法为 {filename_base} 原子发布未占用的输出文件名")


def cleanup_part_file(part_path: Path | None) -> None:
    if part_path is None:
        return
    try:
        part_path.unlink(missing_ok=True)
    except OSError as exc:
        warnings.warn(f"已完成 Markdown 发布，但临时 part 文件清理失败: {part_path}: {exc}", RuntimeWarning)


def export_note(source_file: Path, export_dir: Path, date_override: str | None) -> ExportResult:
    raw_content = source_file.read_text(encoding="utf-8")
    source_encoding_ok, source_encoding_message = validate_utf8_text_file(source_file, require_cjk=True)
    if not source_encoding_ok:
        raise UnicodeError(source_encoding_message)
    meeting_date = normalize_meeting_date(date_override, raw_content)
    title = detect_filename_title(raw_content, source_file.name)
    filename_base = f"{meeting_date} - {title}"
    export_dir = export_dir / meeting_date
    export_dir.mkdir(parents=True, exist_ok=True)

    md_path = export_dir / f"{filename_base}.md"
    part_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{filename_base}.",
            suffix=".part",
            dir=export_dir,
            delete=False,
        ) as destination:
            part_path = Path(destination.name)
            with source_file.open("rb") as source:
                shutil.copyfileobj(source, destination)
        shutil.copystat(source_file, part_path)
        md_ok, md_message = validate_utf8_text_file(part_path, require_cjk=True)
        if not md_ok:
            raise UnicodeError(md_message)
        md_path = publish_completed_file(part_path, export_dir, filename_base)
    except Exception as exc:
        md_ok = False
        md_message = str(exc)
    finally:
        cleanup_part_file(part_path)

    return ExportResult(
        md_path=md_path,
        md_created=md_ok,
        md_message=md_message,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="导出投资会议纪要到 Obsidian 目录（仅 Markdown）")
    parser.add_argument("input_file", help="已整理完成的 Markdown 文件")
    parser.add_argument("--export-dir", default=str(DEFAULT_EXPORT_DIR), help=f"导出目录，默认 {DEFAULT_EXPORT_DIR}")
    parser.add_argument("--meeting-date", help="覆盖系统日期，格式 YYYY-MM-DD")
    parser.add_argument("--mas-summary", required=True, help="当前运行且已完成 draft_review 的 mas_run_summary.json")
    parser.add_argument("--verification", help="可选：verification sidecar JSON/JSONL")
    parser.add_argument("--require-verification", action="store_true", help="要求 verification sidecar 存在且非空")
    parser.add_argument("--timestamp-index", help="可选：timestamp_index.json")
    parser.add_argument("--require-reliable-timestamp-index", action="store_true")
    parser.add_argument("--source-mode", default="auto")
    parser.add_argument("--timestamp-mode", choices=["auto", "reliable", "unavailable"], default="auto")
    parser.add_argument("--require-audio-timestamps", action="store_true")
    args = parser.parse_args()

    source_file = Path(args.input_file).expanduser().resolve()
    if not source_file.exists():
        print(f"输入文件不存在: {source_file}", file=sys.stderr)
        return 1

    export_dir = Path(args.export_dir).expanduser().resolve()
    try:
        run_export_preflight(
            source_file,
            Path(args.mas_summary).expanduser().resolve(),
            verification=Path(args.verification).expanduser().resolve() if args.verification else None,
            require_verification=args.require_verification,
            timestamp_index=Path(args.timestamp_index).expanduser().resolve() if args.timestamp_index else None,
            require_reliable_timestamp_index=args.require_reliable_timestamp_index,
            source_mode=args.source_mode,
            timestamp_mode=args.timestamp_mode,
            require_audio_timestamps=args.require_audio_timestamps,
        )
        result = export_note(source_file, export_dir, args.meeting_date)
    except Exception as exc:
        print(f"Markdown: 未生成 ({exc})", file=sys.stderr)
        return 1

    if result.md_created:
        print(f"Markdown: {result.md_path}")
    else:
        print(f"Markdown: 未生成 ({result.md_message})")
    return 0 if result.md_created else 1


if __name__ == "__main__":
    raise SystemExit(main())
