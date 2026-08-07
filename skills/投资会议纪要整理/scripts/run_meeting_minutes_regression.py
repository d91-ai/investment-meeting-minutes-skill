#!/usr/bin/env python3
"""Run fixed local regression checks for the meeting-minutes contract."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import errno
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import warnings as py_warnings
from pathlib import Path
from typing import Any
from unittest.mock import patch

try:
    import fcntl as _regression_fcntl  # noqa: F401
except ModuleNotFoundError:
    # Production keeps native POSIX locking; the regression harness supplies a
    # process-local equivalent so deterministic tests can also run on Windows.
    _regression_lock = threading.RLock()
    _regression_fcntl = types.ModuleType("fcntl")
    _regression_fcntl.LOCK_SH = 1
    _regression_fcntl.LOCK_EX = 2
    _regression_fcntl.LOCK_UN = 8

    def _regression_flock(_fd: int, operation: int) -> None:
        if operation == _regression_fcntl.LOCK_UN:
            _regression_lock.release()
        else:
            _regression_lock.acquire()

    _regression_fcntl.flock = _regression_flock
    sys.modules["fcntl"] = _regression_fcntl

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_DIR = SCRIPT_DIR.parent
DEFAULT_CASES_PATH = SKILL_DIR / "references/regression_samples/cases.json"


def utf8_subprocess_env() -> dict[str, str]:
    """Make Python child-process output deterministic across host locales."""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


sys.path.insert(0, str(SCRIPT_DIR))
from validate_meeting_minutes_contract import (  # noqa: E402
    inline_doubtful_mapping_findings,
    validate_contract,
    validate_timestamp_index_file,
    validate_verification_sidecar,
)
from validate_mas_artifacts import (  # noqa: E402
    file_sha256,
    validate_file as validate_mas_artifacts_file,
    validate_payload as validate_mas_artifacts_payload,
)
from build_mas_task_bundle import (  # noqa: E402
    build_bundle_from_request as build_mas_task_bundle_from_request,
    skill_instruction_sha256,
    validate_bundle as validate_mas_task_bundle,
    write_dispatch_files as write_mas_dispatch_files,
)
from create_mas_source_manifest import create_source_manifest, source_manifest_artifact  # noqa: E402
from summarize_mas_decisions import summarize_file as summarize_mas_decision_file  # noqa: E402
from collect_mas_artifacts import collect_mas_run  # noqa: E402
from assemble_speaker_turn_edits import assemble_speaker_turn_edits  # noqa: E402
from build_speaker_turn_manifest import build_manifest as build_speaker_turn_manifest  # noqa: E402
from ingest_mas_artifact import expand_speaker_edit_response, ingest_mas_artifact_file  # noqa: E402
import ingest_mas_artifact as ingest_mas_artifact_module  # noqa: E402
from plan_mas_next_action import plan_from_summary  # noqa: E402
from prepare_draft_review import prepare_draft_review  # noqa: E402
from run_mas_phase_operator import DEFAULT_MAX_PARALLEL, run_mas_phase_operator  # noqa: E402
from run_mas_dry_run import (  # noqa: E402
    run_mas_dry_run,
    synthetic_final_markdown,
    synthetic_verification_payload,
)
from record_mas_main_actions import record_main_actions  # noqa: E402
from archive_raw_inputs import archive_files  # noqa: E402
from export_to_obsidian import export_note, file_sha256 as export_file_sha256, validate_mas_draft_gate  # noqa: E402
import archive_raw_inputs as archive_module  # noqa: E402
import export_to_obsidian as export_module  # noqa: E402
from process_transcript import build_output, detect_segments  # noqa: E402
from sensevoice_transcription_server import (  # noqa: E402
    MultipartForm,
    UploadedFormFile,
    _run_transcribe as run_sensevoice_subprocess,
    require_audio_form_file,
)
import transcribe_audio as transcribe_audio_module  # noqa: E402


def case_series_config(case: dict[str, Any], base_dir: Path) -> Path:
    raw = str(case.get("series_config_file") or "").strip()
    return (base_dir / raw).resolve() if raw else export_module.DEFAULT_SERIES_CONFIG


def read_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise ValueError(f"回归样例格式错误: {path}")
    return cases


@contextlib.contextmanager
def windows_subprocess_fcntl_shim() -> Any:
    if os.name != "nt":
        yield
        return
    previous_pythonpath = os.environ.get("PYTHONPATH")
    with tempfile.TemporaryDirectory(prefix="meeting-minutes-fcntl-shim-") as tmpdir:
        shim_path = Path(tmpdir) / "fcntl.py"
        shim_path.write_text(
            "LOCK_SH = 1\nLOCK_EX = 2\nLOCK_UN = 8\ndef flock(_fd, _operation):\n    return None\n",
            encoding="utf-8",
        )
        os.environ["PYTHONPATH"] = os.pathsep.join(
            item for item in (tmpdir, previous_pythonpath or "") if item
        )
        try:
            yield
        finally:
            if previous_pythonpath is None:
                os.environ.pop("PYTHONPATH", None)
            else:
                os.environ["PYTHONPATH"] = previous_pythonpath


def dispatch_context(task_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    bundle = json.loads((task_dir / "mas_task_bundle.json").read_text(encoding="utf-8"))
    manifest = json.loads((task_dir / "dispatch_manifest.json").read_text(encoding="utf-8"))
    if not isinstance(bundle, dict) or not isinstance(manifest, dict):
        raise ValueError("MAS regression dispatch context must contain JSON objects")
    return bundle, manifest


def fixture_identity(manifest: dict[str, Any], artifact_type: str) -> dict[str, Any]:
    run_id = str(manifest.get("run_id") or "")
    if artifact_type == "source_manifest":
        return {
            "run_id": run_id,
            "task_id": f"{run_id}:main:source_manifest",
            "dispatch_phase": "pre_draft",
            "artifact_owner": "Main Orchestrator",
        }
    for task in manifest.get("task_files", []):
        if not isinstance(task, dict):
            continue
        produced = {str(task.get("artifact_type") or "")}
        produced.update(str(item) for item in task.get("secondary_artifacts", []))
        if artifact_type in produced:
            return {
                "run_id": run_id,
                "task_id": str(task.get("task_id") or ""),
                "dispatch_phase": str(task.get("dispatch_phase") or ""),
                "artifact_owner": str(task.get("artifact_owner") or task.get("role") or ""),
            }
    raise ValueError(f"MAS regression cannot resolve artifact task identity: {artifact_type}")


def fixture_payload(
    manifest: dict[str, Any],
    artifact_type: str,
    artifact: Any,
    markdown_path: Path | None = None,
) -> dict[str, Any]:
    artifact_value = json.loads(json.dumps(artifact, ensure_ascii=False))
    if artifact_type == "export_manifest" and isinstance(artifact_value, dict) and markdown_path is not None:
        artifact_value["markdown_path"] = str(markdown_path)
        artifact_value["markdown_sha256"] = file_sha256(markdown_path)
        artifact_value["main_actions_verified"] = True
    if artifact_type in {"target_attribution_review", "fidelity_review"} and isinstance(artifact_value, dict) and markdown_path is not None:
        artifact_value["reviewed_markdown_path"] = str(markdown_path)
        artifact_value["reviewed_markdown_sha256"] = file_sha256(markdown_path)
    return {
        **fixture_identity(manifest, artifact_type),
        "artifact_type": artifact_type,
        "artifact": artifact_value,
    }


def bind_fixture_return(source_path: Path, destination: Path, manifest: dict[str, Any]) -> Path:
    payload = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"MAS fixture return must be a JSON object: {source_path}")
    if isinstance(payload.get("artifacts"), dict):
        artifact_types = [str(item) for item in payload["artifacts"]]
    else:
        artifact_types = [str(payload.get("artifact_type") or "")]
    primary = next((item for item in artifact_types if item and item != "doubtful_items"), artifact_types[0])
    identity = fixture_identity(manifest, primary)
    identity.pop("task_artifact_set", None)
    identity.pop("ingested_split", None)
    bound = {**identity, **payload}
    destination.write_text(json.dumps(bound, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination


def fixture_return_payload(
    manifest: dict[str, Any],
    primary_artifact: str,
    fixture_artifacts: dict[str, Any],
    markdown_path: Path | None = None,
) -> dict[str, Any]:
    identity = fixture_identity(manifest, primary_artifact)
    task_artifact_set = [primary_artifact]
    for task in manifest.get("task_files", []):
        if isinstance(task, dict) and str(task.get("task_id") or "") == str(identity.get("task_id") or ""):
            task_artifact_set = [str(task.get("artifact_type") or "")]
            task_artifact_set.extend(str(item) for item in task.get("secondary_artifacts", []))
            break
    values: dict[str, Any] = {}
    for artifact_type in task_artifact_set:
        if artifact_type not in fixture_artifacts:
            raise ValueError(f"MAS return fixture missing task artifact: {artifact_type}")
        artifact_value = json.loads(json.dumps(fixture_artifacts[artifact_type], ensure_ascii=False))
        if artifact_type == "export_manifest" and isinstance(artifact_value, dict) and markdown_path is not None:
            artifact_value["markdown_path"] = str(markdown_path)
            artifact_value["markdown_sha256"] = file_sha256(markdown_path)
            artifact_value["main_actions_verified"] = True
        if artifact_type in {"target_attribution_review", "fidelity_review"} and isinstance(artifact_value, dict) and markdown_path is not None:
            artifact_value["reviewed_markdown_path"] = str(markdown_path)
            artifact_value["reviewed_markdown_sha256"] = file_sha256(markdown_path)
        values[artifact_type] = artifact_value
    if len(values) == 1:
        artifact_type, artifact_value = next(iter(values.items()))
        return {**identity, "artifact_type": artifact_type, "artifact": artifact_value}
    return {**identity, "artifacts": values}


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
    elif case.get("check") == "draft_review_preflight":
        verification_path = (
            base_dir / str(case["verification_file"])
            if case.get("verification_file")
            else None
        )
        preflight = prepare_draft_review(
            file_path,
            verification_path=verification_path,
            source_mode=str(case.get("source_mode") or "auto"),
        )
        expected_ready = bool(case.get("expect_ready"))
        actual_ready = bool(preflight.get("ready_for_semantic_review"))
        errors = []
        if actual_ready != expected_ready:
            errors.append(
                f"draft-review preflight readiness mismatch: expected={expected_ready} actual={actual_ready}"
            )
        if actual_ready and not str(preflight.get("markdown_sha256") or ""):
            errors.append("passing draft-review preflight did not freeze a Markdown SHA-256")
        if not actual_ready and str(preflight.get("markdown_sha256") or ""):
            errors.append("failed draft-review preflight unexpectedly emitted a frozen SHA-256")
        result = {
            **preflight,
            "ok": not errors,
            "errors": errors,
            "preflight_errors": preflight.get("errors", []),
        }
    elif case.get("check") == "export_mas_draft_gate":
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-export-gate-") as tmpdir:
            task_dir = Path(tmpdir)
            artifact_path = task_dir / "fidelity_review.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "artifact_type": "fidelity_review",
                        "artifact": {
                            "reviewed_markdown_path": str(file_path.resolve()),
                            "reviewed_markdown_sha256": export_file_sha256(file_path.resolve()),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            summary_path = task_dir / "mas_run_summary.json"
            summary = {
                "ok": True,
                "task_dir": str(task_dir),
                "phase_gates": [{"phase": "draft_review", "status": "complete"}],
                "next_action": {"type": "collect_or_dispatch_phase_artifacts", "phase": "final_verification"},
                "artifact_types": ["fidelity_review"],
                "artifact_sources": [{"artifact_type": "fidelity_review", "path": str(artifact_path)}],
            }
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            valid_result = validate_mas_draft_gate(summary_path, file_path)
            summary["next_action"] = {
                "type": "apply_main_actions_before_final_verification",
                "phase": "draft_review",
            }
            summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            blocked_result = validate_mas_draft_gate(summary_path, file_path)
            errors = []
            if valid_result.get("ok") is not True:
                errors.append(f"有效 MAS draft_review 门禁被错误拒绝: {valid_result}")
            if blocked_result.get("ok") is not False:
                errors.append("仍有 main actions 的 MAS summary 未阻断导出")
            result = {"ok": not errors, "errors": errors, "warnings": []}
    elif case.get("check") == "export_filename":
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-export-") as tmpdir:
            errors = []
            warnings = []
            md_path = ""
            try:
                result_export = export_note(
                    file_path,
                    Path(tmpdir),
                    str(case["meeting_date_override"]) if case.get("meeting_date_override") else None,
                    case_series_config(case, base_dir),
                )
                expected_stem = str(case["expected_stem"])
                actual_stem = result_export.md_path.stem
                md_path = str(result_export.md_path)
                if actual_stem != expected_stem:
                    errors.append(f"导出文件名不符合预期: expected={expected_stem} actual={actual_stem}")
                if result_export.md_path.suffix != ".md":
                    errors.append(f"Markdown 后缀错误: {result_export.md_path.name}")
                if not result_export.md_created:
                    errors.append(f"Markdown 未生成: {result_export.md_message}")
            except Exception as exc:
                errors.append(f"导出失败: {exc}")
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": warnings,
                "md_path": md_path,
            }
    elif case.get("check") == "export_concurrent":
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-export-concurrent-") as tmpdir:
            errors = []
            warnings = []
            count = int(case.get("count") or 8)
            with ThreadPoolExecutor(max_workers=count) as executor:
                exports = list(
                    executor.map(
                        lambda _: export_note(
                            file_path,
                            Path(tmpdir),
                            str(case.get("meeting_date_override") or ""),
                            case_series_config(case, base_dir),
                        ),
                        range(count),
                    )
                )
            paths = [item.md_path for item in exports]
            if not all(item.md_created for item in exports):
                errors.append("并发导出存在未成功结果")
            if len(set(paths)) != count:
                errors.append(f"并发导出路径不唯一: expected={count} actual={len(set(paths))}")
            source_bytes = file_path.read_bytes()
            for path in paths:
                if not path.is_file() or path.read_bytes() != source_bytes:
                    errors.append(f"并发导出文件缺失或内容不一致: {path}")
            if list(Path(tmpdir).rglob("*.part")):
                errors.append("并发导出后残留隐藏 part 文件")
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": warnings,
                "export_count": count,
                "unique_path_count": len(set(paths)),
            }
    elif case.get("check") == "export_part_cleanup_failure":
        errors = []
        warnings = []
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-export-cleanup-") as tmpdir:
            original_unlink = Path.unlink

            def fail_part_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
                if path.suffix == ".part":
                    raise PermissionError("synthetic part cleanup failure")
                original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_part_unlink), py_warnings.catch_warnings(record=True) as caught:
                py_warnings.simplefilter("always")
                exported = export_note(
                    file_path,
                    Path(tmpdir),
                    str(case.get("meeting_date_override") or ""),
                    case_series_config(case, base_dir),
                )
            if not exported.md_created or not exported.md_path.is_file():
                errors.append("part 清理失败不应反转已完成的 Markdown 发布")
            elif exported.md_path.read_bytes() != file_path.read_bytes():
                errors.append("part 清理失败后的 Markdown 内容不完整")
            part_files = list(Path(tmpdir).rglob("*.part"))
            if not part_files or not any("part 文件清理失败" in str(item.message) for item in caught):
                errors.append("part 清理失败缺少明确告警或故障注入未生效")
            for part_file in part_files:
                original_unlink(part_file, missing_ok=True)
        result = {"ok": not errors, "errors": errors, "warnings": warnings}
    elif case.get("check") == "archive_part_cleanup_failure":
        errors = []
        warnings = []
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-archive-cleanup-") as tmpdir:
            original_unlink = Path.unlink

            def fail_part_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
                if path.suffix == ".part":
                    raise PermissionError("synthetic part cleanup failure")
                original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", fail_part_unlink), py_warnings.catch_warnings(record=True) as caught:
                py_warnings.simplefilter("always")
                archived = archive_files(
                    [file_path],
                    Path(tmpdir),
                    str(case.get("archive_date") or "2026-07-01"),
                    str(case.get("meeting_title") or "合成归档"),
                )
            if len(archived) != 1 or not archived[0].is_file():
                errors.append("part 清理失败不应反转已完成的原始文件归档")
            elif archived[0].read_bytes() != file_path.read_bytes():
                errors.append("part 清理失败后的归档内容不完整")
            part_files = list(Path(tmpdir).rglob("*.part"))
            if not part_files or not any("part 文件清理失败" in str(item.message) for item in caught):
                errors.append("归档 part 清理失败缺少明确告警或故障注入未生效")
            for part_file in part_files:
                original_unlink(part_file, missing_ok=True)
        result = {"ok": not errors, "errors": errors, "warnings": warnings}
    elif case.get("check") == "atomic_publish_unsupported":
        errors = []
        warnings = []
        unsupported = OSError(errno.EOPNOTSUPP, "synthetic hard-link unsupported")
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-atomic-unsupported-") as tmpdir:
            export_dir = Path(tmpdir) / "export"
            archive_dir = Path(tmpdir) / "archive"
            with patch.object(export_module.os, "link", side_effect=unsupported):
                exported = export_note(
                    file_path,
                    export_dir,
                    str(case.get("meeting_date_override") or ""),
                    case_series_config(case, base_dir),
                )
            if exported.md_created or "不支持安全的原子无覆盖发布" not in exported.md_message:
                errors.append("不支持 hard-link 时 Markdown 导出未明确 fail closed")
            if list(export_dir.rglob("*.md")) or list(export_dir.rglob("*.part")):
                errors.append("Markdown 原子发布失败后残留 final 或 part 文件")
            archive_error = ""
            try:
                with patch.object(archive_module.os, "link", side_effect=unsupported):
                    archive_files(
                        [file_path],
                        archive_dir,
                        str(case.get("archive_date") or "2026-07-01"),
                        str(case.get("meeting_title") or "合成归档"),
                    )
            except OSError as exc:
                archive_error = str(exc)
            if "不支持安全的原子无覆盖归档" not in archive_error:
                errors.append("不支持 hard-link 时原始文件归档未明确 fail closed")
            if list(archive_dir.rglob("*.md")) or list(archive_dir.rglob("*.part")):
                errors.append("原始文件原子归档失败后残留 final 或 part 文件")
        result = {"ok": not errors, "errors": errors, "warnings": warnings}
    elif case.get("check") == "archive_concurrent":
        with tempfile.TemporaryDirectory(prefix="meeting-minutes-archive-concurrent-") as tmpdir:
            errors = []
            count = int(case.get("count") or 8)
            with ThreadPoolExecutor(max_workers=count) as executor:
                archived_batches = list(
                    executor.map(
                        lambda _: archive_files(
                            [file_path],
                            Path(tmpdir),
                            str(case.get("archive_date") or "2026-07-01"),
                            str(case.get("meeting_title") or "synthetic concurrent archive"),
                        ),
                        range(count),
                    )
                )
            paths = [batch[0] for batch in archived_batches]
            if len(set(paths)) != count:
                errors.append(f"并发归档路径不唯一: expected={count} actual={len(set(paths))}")
            source_bytes = file_path.read_bytes()
            for path in paths:
                if not path.is_file() or path.read_bytes() != source_bytes:
                    errors.append(f"并发归档文件缺失或内容不一致: {path}")
            if list(Path(tmpdir).rglob("*.part")):
                errors.append("并发归档后残留隐藏 part 文件")
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": [],
                "archive_count": count,
                "unique_path_count": len(set(paths)),
            }
    elif case.get("check") == "process_transcript":
        input_text = str(case.get("input_text") or "")
        errors = []
        warnings = []
        segments: list[tuple[str, str]] = []
        output = ""
        if not input_text.strip():
            errors.append("输入文本为空，无法预处理会议转录")
        else:
            preferred_speakers = [
                str(item)
                for item in case.get("preferred_speakers", [])
                if str(item).strip()
            ]
            segments = detect_segments(input_text, preferred_speakers or None)
            output = build_output(segments, input_text)
            expected_segment_count = case.get("expected_segment_count")
            if expected_segment_count is not None and len(segments) != int(expected_segment_count):
                errors.append(f"预处理发言段数不符合预期: expected={expected_segment_count} actual={len(segments)}")
            for term in [str(term) for term in case.get("required_terms", [])]:
                if term not in output:
                    errors.append(f"缺少预处理输出锚点: {term}")
            for term in [str(term) for term in case.get("forbidden_terms", [])]:
                if term in output:
                    errors.append(f"包含预处理禁止锚点: {term}")
        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "segment_count": len(segments),
        }
    elif case.get("check") == "sensevoice_bridge_form":
        errors = []
        warnings = []
        form = MultipartForm()
        if case.get("include_audio"):
            form.add_file("audio", UploadedFormFile(str(case.get("filename") or "sample.wav"), b""))
        try:
            require_audio_form_file(form)
        except ValueError as exc:
            errors.append(str(exc))
        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
        }
    elif case.get("check") == "sensevoice_empty_result":
        with tempfile.TemporaryDirectory(prefix="sensevoice-empty-result-") as tmpdir:
            ok, text, error = run_sensevoice_subprocess(
                [sys.executable, "-c", "pass"],
                Path(tmpdir),
                "input",
                timeout=5,
            )
        errors = []
        if ok or text or "without usable text" not in error:
            errors.append(f"SenseVoice bridge 未拒绝空转写: ok={ok} text={text!r} error={error!r}")
        result = {"ok": not errors, "errors": errors, "warnings": []}
    elif case.get("check") == "sensevoice_managed_outputs":
        errors = []
        with tempfile.TemporaryDirectory(prefix="sensevoice-managed-outputs-") as tmpdir:
            output_dir = Path(tmpdir) / "out"
            output_dir.mkdir()
            input_file = Path(tmpdir) / "input.wav"
            input_file.write_bytes(b"synthetic")
            managed_paths = [
                output_dir / f"input{suffix}"
                for suffix in transcribe_audio_module.SENSEVOICE_MANAGED_OUTPUT_SUFFIXES
            ]
            for path in managed_paths:
                path.write_text("OLD OUTPUT\n", encoding="utf-8")
            fake_funasr = types.ModuleType("funasr")
            fake_funasr.AutoModel = lambda **_: object()  # type: ignore[attr-defined]
            shared_patches = (
                patch.dict(sys.modules, {"funasr": fake_funasr}),
                patch.object(transcribe_audio_module, "_ensure_ffmpeg_for_current_process", lambda: None),
                patch.object(
                    transcribe_audio_module,
                    "_resolve_model_ref",
                    lambda model_name, **_: model_name,
                ),
                patch.object(transcribe_audio_module, "_select_device", lambda _: "cpu"),
            )
            with shared_patches[0], shared_patches[1], shared_patches[2], shared_patches[3], patch.object(
                transcribe_audio_module,
                "_run_sensevoice_vad_segments",
                return_value={"text": "", "sentence_info": [], "timestamp_index": [], "raw": []},
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                empty_code = transcribe_audio_module._run_sensevoice(
                    input_file,
                    output_dir,
                    "iic/SenseVoiceSmall",
                    "zh",
                    "all",
                    False,
                    False,
                    "none",
                    "",
                    False,
                )
            if empty_code != 1 or any(path.read_text(encoding="utf-8") != "OLD OUTPUT\n" for path in managed_paths):
                errors.append("SenseVoice 空结果失败后未保留上一轮完整输出")

            for path in managed_paths:
                path.write_text("OLD OUTPUT\n", encoding="utf-8")
            with patch.dict(sys.modules, {"funasr": fake_funasr}), patch.object(
                transcribe_audio_module,
                "_ensure_ffmpeg_for_current_process",
                lambda: None,
            ), patch.object(
                transcribe_audio_module,
                "_resolve_model_ref",
                lambda model_name, **_: model_name,
            ), patch.object(
                transcribe_audio_module,
                "_select_device",
                lambda _: "cpu",
            ), patch.object(
                transcribe_audio_module,
                "_run_sensevoice_vad_segments",
                return_value={"text": "新转写", "sentence_info": [], "timestamp_index": [], "raw": []},
            ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                success_code = transcribe_audio_module._run_sensevoice(
                    input_file,
                    output_dir,
                    "iic/SenseVoiceSmall",
                    "zh",
                    "all",
                    False,
                    False,
                    "none",
                    "",
                    False,
                )
            expected_present = {output_dir / "input.txt", output_dir / "input.json"}
            expected_absent = set(managed_paths) - expected_present
            if (
                success_code != 0
                or any(not path.is_file() for path in expected_present)
                or any(path.exists() for path in expected_absent)
                or (output_dir / "input.txt").read_text(encoding="utf-8").strip() != "新转写"
            ):
                errors.append("SenseVoice 成功结果未清除本轮未产生的旧 side outputs")
        result = {"ok": not errors, "errors": errors, "warnings": []}
    elif case.get("check") == "sensevoice_output_transaction":
        errors = []
        with tempfile.TemporaryDirectory(prefix="sensevoice-output-transaction-") as tmpdir:
            output_dir = Path(tmpdir)
            stem = "input"
            old_contents = {
                suffix: f"OLD {suffix}\n"
                for suffix in transcribe_audio_module.SENSEVOICE_MANAGED_OUTPUT_SUFFIXES
            }
            for suffix, content in old_contents.items():
                (output_dir / f"{stem}{suffix}").write_text(content, encoding="utf-8")
            original_replace = os.replace

            def fail_second_publish(source: Any, destination: Any) -> None:
                source_path = Path(source)
                destination_path = Path(destination)
                if source_path.parent.name == "stage" and destination_path.name == f"{stem}.json":
                    raise OSError("synthetic commit failure")
                original_replace(source, destination)

            try:
                with patch.object(transcribe_audio_module.os, "replace", fail_second_publish):
                    transcribe_audio_module._commit_sensevoice_outputs(
                        output_dir,
                        stem,
                        {".txt": "NEW TXT\n", ".json": "NEW JSON\n"},
                    )
            except OSError:
                pass
            else:
                errors.append("SenseVoice 事务提交故障注入未触发")
            for suffix, content in old_contents.items():
                path = output_dir / f"{stem}{suffix}"
                if not path.is_file() or path.read_text(encoding="utf-8") != content:
                    errors.append(f"SenseVoice 提交失败后未恢复旧输出: {suffix}")
            if list(output_dir.glob(f".{stem}.sensevoice-txn-*")):
                errors.append("SenseVoice 提交失败后残留 transaction 目录")

            child_code = "\n".join(
                [
                    "import os, sys",
                    "if os.name == 'nt':",
                    "    import threading, types",
                    "    lock = threading.RLock()",
                    "    shim = types.ModuleType('fcntl')",
                    "    shim.LOCK_SH, shim.LOCK_EX, shim.LOCK_UN = 1, 2, 8",
                    "    shim.flock = lambda _fd, op: lock.release() if op == shim.LOCK_UN else lock.acquire()",
                    "    sys.modules['fcntl'] = shim",
                    "from pathlib import Path",
                    "import transcribe_audio as target",
                    "output_dir = Path(sys.argv[1])",
                    "stem = sys.argv[2]",
                    "original_replace = target.os.replace",
                    "def abrupt_replace(source, destination):",
                    "    original_replace(source, destination)",
                    "    if Path(source).parent.name == 'stage':",
                    "        os._exit(77)",
                    "target.os.replace = abrupt_replace",
                    "target._commit_sensevoice_outputs(output_dir, stem, {'.txt': 'CRASH TXT\\n', '.json': 'CRASH JSON\\n'})",
                ]
            )
            child_env = utf8_subprocess_env()
            existing_pythonpath = child_env.get("PYTHONPATH", "")
            child_env["PYTHONPATH"] = os.pathsep.join(
                item for item in (str(SCRIPT_DIR), existing_pythonpath) if item
            )
            crashed = subprocess.run(
                [sys.executable, "-c", child_code, str(output_dir), stem],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=child_env,
                timeout=10,
            )
            if crashed.returncode != 77:
                errors.append(
                    f"SenseVoice abrupt-exit 故障注入未按预期退出: returncode={crashed.returncode} stderr={crashed.stderr}"
                )
            with transcribe_audio_module._sensevoice_stem_lock(output_dir, stem):
                transcribe_audio_module._recover_sensevoice_transactions(output_dir, stem)
            for suffix, content in old_contents.items():
                path = output_dir / f"{stem}{suffix}"
                if not path.is_file() or path.read_text(encoding="utf-8") != content:
                    errors.append(f"SenseVoice abrupt-exit 恢复后旧输出不完整: {suffix}")
            if list(output_dir.glob(f".{stem}.sensevoice-txn-*")):
                errors.append("SenseVoice abrupt-exit 恢复后残留 transaction 目录")

            # 方括号在 POSIX 与 Windows 文件名中均合法，同时仍可验证 glob.escape 隔离。
            victim_stem = "fooa"
            metachar_stem = "foo[a]"
            for suffix, content in old_contents.items():
                (output_dir / f"{victim_stem}{suffix}").write_text(content, encoding="utf-8")
            victim_crash = subprocess.run(
                [sys.executable, "-c", child_code, str(output_dir), victim_stem],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=child_env,
                timeout=10,
            )
            if victim_crash.returncode != 77:
                errors.append("SenseVoice metachar stem 隔离故障注入未按预期退出")
            with transcribe_audio_module._sensevoice_stem_lock(output_dir, metachar_stem):
                transcribe_audio_module._recover_sensevoice_transactions(output_dir, metachar_stem)
            if not list(output_dir.glob(f".{victim_stem}.sensevoice-txn-*")):
                errors.append("含 glob 元字符的 stem 错误消费了其他 stem 的遗留事务")
            if any((output_dir / f"{metachar_stem}{suffix}").exists() for suffix in old_contents):
                errors.append("含 glob 元字符的 stem 错误生成了跨 stem 恢复输出")
            with transcribe_audio_module._sensevoice_stem_lock(output_dir, victim_stem):
                transcribe_audio_module._recover_sensevoice_transactions(output_dir, victim_stem)
            for suffix, content in old_contents.items():
                path = output_dir / f"{victim_stem}{suffix}"
                if not path.is_file() or path.read_text(encoding="utf-8") != content:
                    errors.append(f"SenseVoice metachar stem 隔离后 victim 恢复失败: {suffix}")

            active = 0
            max_active = 0
            counter_lock = threading.Lock()

            def lock_probe() -> None:
                nonlocal active, max_active
                with transcribe_audio_module._sensevoice_stem_lock(output_dir, stem):
                    with counter_lock:
                        active += 1
                        max_active = max(max_active, active)
                    time.sleep(0.02)
                    with counter_lock:
                        active -= 1

            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(lambda _: lock_probe(), range(2)))
            if max_active != 1:
                errors.append(f"同 stem SenseVoice 运行未串行化: max_active={max_active}")
        result = {"ok": not errors, "errors": errors, "warnings": []}
    elif case.get("check") == "mas_artifacts":
        result = validate_mas_artifacts_file(
            file_path,
            required_artifacts=[str(item) for item in case.get("require_artifacts", [])],
        )
    elif case.get("check") == "mas_source_manifest":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS source_manifest request 必须是 JSON object: {file_path}")
        manifest, warnings = create_source_manifest(
            request_payload,
            archive_allowed=bool(case.get("archive_allowed", False)),
            archive_status=str(case.get("archive_status") or "not_started"),
            skipped_reason=str(case.get("skipped_reason") or ""),
        )
        artifact = source_manifest_artifact(manifest, "regression-source-manifest")
        result = validate_mas_artifacts_payload(artifact, required_artifacts=["source_manifest"])
        result["warnings"] = [str(warning) for warning in warnings] + result["warnings"]
        expected_source_mode = case.get("expect_source_mode")
        if expected_source_mode and manifest.get("source_mode") != expected_source_mode:
            result["errors"].append(
                f"MAS source_manifest source_mode 不符合预期: expected={expected_source_mode} "
                f"actual={manifest.get('source_mode')}"
            )
            result["ok"] = False
        material_kinds = {
            str(item.get("kind") or "")
            for item in manifest.get("materials", [])
            if isinstance(item, dict)
        }
        for kind in [str(item) for item in case.get("expect_material_kinds", [])]:
            if kind not in material_kinds:
                result["errors"].append(f"MAS source_manifest 缺少 material kind: {kind}")
                result["ok"] = False
        if "expect_archive_allowed" in case and manifest.get("archive_allowed") != bool(case["expect_archive_allowed"]):
            result["errors"].append(
                "MAS source_manifest archive_allowed 不符合预期: "
                f"expected={bool(case['expect_archive_allowed'])} actual={manifest.get('archive_allowed')}"
            )
            result["ok"] = False
        manifest_text = json.dumps(artifact, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in manifest_text:
                result["errors"].append(f"MAS source_manifest 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_source_manifest_cli_binding":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS source_manifest request 必须是 JSON object: {file_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        conflicting_request = base_dir / str(case["conflicting_request_file"])
        with tempfile.TemporaryDirectory(prefix="mas-source-manifest-cli-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "create_mas_source_manifest.py"),
                    "--task-dir",
                    str(task_dir),
                    "--request-json",
                    str(conflicting_request),
                    "--json",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=utf8_subprocess_env(),
                timeout=20,
                check=False,
            )
            errors = []
            artifact_path = task_dir / "artifacts" / "source_manifest.json"
            if completed.returncode != 0 or not artifact_path.is_file():
                errors.append(
                    "MAS source_manifest CLI 未成功生成绑定 artifact: "
                    f"returncode={completed.returncode} output={completed.stdout}{completed.stderr}"
                )
            else:
                bound_bundle = json.loads((task_dir / "mas_task_bundle.json").read_text(encoding="utf-8"))
                artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
                artifact_manifest = artifact_payload.get("artifact", {})
                if artifact_payload.get("run_id") != bound_bundle.get("run_id"):
                    errors.append("MAS source_manifest CLI 写入了非当前 dispatch run_id")
                expected_materials = {
                    str(item.get("name") or "")
                    for item in create_source_manifest(bound_bundle)[0].get("materials", [])
                    if isinstance(item, dict)
                }
                actual_materials = {
                    str(item.get("name") or "")
                    for item in artifact_manifest.get("materials", [])
                    if isinstance(item, dict)
                }
                if actual_materials != expected_materials:
                    errors.append(
                        "MAS source_manifest CLI 材料未绑定当前 dispatch: "
                        f"expected={sorted(expected_materials)} actual={sorted(actual_materials)}"
                    )
            result = {"ok": not errors, "errors": errors, "warnings": []}
    elif case.get("check") == "speaker_editing_mas":
        errors = []
        warnings: list[str] = []
        source_text = file_path.read_text(encoding="utf-8")
        speaker_manifest = build_speaker_turn_manifest(file_path, source_text, max_chars=8000)
        expected_shard_turns = [["turn_000001", "turn_000002", "turn_000003"]]
        actual_shard_turns = [
            [str(turn_id) for turn_id in shard.get("turn_ids", [])]
            for shard in speaker_manifest.get("shards", [])
            if isinstance(shard, dict)
        ]
        if actual_shard_turns != expected_shard_turns:
            errors.append("相邻完整 speaker turns 未按容量组成同一工作包")
        if [
            turn.get("speaker_id")
            for turn in speaker_manifest.get("turns", [])
            if isinstance(turn, dict)
        ] != [
            "speaker_001",
            "speaker_002",
            "speaker_001",
        ]:
            errors.append("speaker turns 未保持 A→B→A 全局回场身份")
        if [
            shard.get("speaker_ids")
            for shard in speaker_manifest.get("shards", [])
            if isinstance(shard, dict)
        ] != [["speaker_001", "speaker_002"]]:
            errors.append("工作包 speaker_ids 未与 turns 首次出现顺序一致")

        group_source = "\n".join(
            [
                "宏观策略组",
                "我认为海外流动性仍需观察。",
                "固收资配组",
                "我们维持当前久期，暂不提高杠杆。",
                "有色组",
                "我觉得铜价可能改善，但还要看库存。",
            ]
        )
        group_manifest = build_speaker_turn_manifest(
            Path("group-speaker-layout.txt"),
            group_source,
            max_chars=100,
        )
        group_turns = [
            (
                str(turn.get("speaker_label") or ""),
                str(turn.get("text") or ""),
            )
            for turn in group_manifest.get("turns", [])
            if isinstance(turn, dict)
        ]
        if group_turns != [
            ("宏观策略组", "我认为海外流动性仍需观察。"),
            ("固收资配组", "我们维持当前久期，暂不提高杠杆。"),
            ("有色组", "我觉得铜价可能改善，但还要看库存。"),
        ]:
            errors.append("独立 ××组 标题未被识别为不同 speaker turns")
        if [
            shard.get("speaker_labels")
            for shard in group_manifest.get("shards", [])
            if isinstance(shard, dict)
        ] != [["宏观策略组", "固收资配组", "有色组"]]:
            errors.append("多 speaker 工作包未保留研究组身份和顺序")
        markdown_group_manifest = build_speaker_turn_manifest(
            Path("group-speaker-markdown-layout.md"),
            group_source.replace("宏观策略组", "### 宏观策略组")
            .replace("固收资配组", "### 固收资配组")
            .replace("有色组", "### 有色组"),
            max_chars=100,
        )
        markdown_group_labels = [
            str(turn.get("speaker_label") or "")
            for turn in markdown_group_manifest.get("turns", [])
            if isinstance(turn, dict)
        ]
        if markdown_group_labels != ["宏观策略组", "固收资配组", "有色组"]:
            errors.append("Markdown `### ××组` 标题未被识别为不同 speaker turns")
        request_payload = {
            "run_profile": "standard",
            "source_mode": "document_only",
            "meeting_type": "多人复盘会",
            "risk_flags": ["long_transcript", "filler_cleanup"],
            "materials": [{"kind": "document", "name": file_path.name}],
            "speaker_turn_manifest": speaker_manifest,
            "speaker_editing_mode": "full",
        }
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors.extend(validate_mas_task_bundle(bundle))
        edit_tasks = [
            task
            for task in bundle.get("tasks", [])
            if isinstance(task, dict) and task.get("artifact_schema") == "speaker_turn_edit"
        ]
        if len(edit_tasks) != speaker_manifest.get("shard_count") or len(edit_tasks) != 1:
            errors.append("speaker editing 未按容量工作包生成预期数量的唯一 tasks")
        edit_types = [str(task.get("artifact_type") or "") for task in edit_tasks]
        if len(edit_types) != len(set(edit_types)):
            errors.append("speaker editing artifact_type 不唯一")
        if any(task.get("dispatch_phase") != "editing" for task in edit_tasks):
            errors.append("speaker editing task 未进入 editing phase")
        expected_skill_digest = skill_instruction_sha256()
        for task in edit_tasks:
            prompt = str(task.get("prompt") or "")
            if task.get("skill_instruction_sha256") != expected_skill_digest:
                errors.append("Speaker Editor task 未绑定当前基础 SKILL.md SHA-256")
            if expected_skill_digest in prompt:
                errors.append("Speaker Editor prompt 仍向编辑 Agent 暴露编排 hash")
            if "exact same investment-meeting-minutes SKILL.md" not in prompt:
                errors.append("Speaker Editor prompt 未要求加载同一基础 Skill")
            if "Apply the base skill editing contract exactly:" in prompt:
                errors.append("Speaker Editor prompt 仍复制第二套文本编辑契约")
            if "deletion-only" in prompt.lower() or "subsequence" in prompt.lower():
                errors.append("Speaker Editor prompt 仍包含 deletion-only 字符门禁")
            if "Assigned task_context JSON follows:" in prompt:
                errors.append("Speaker Editor prompt 仍重复序列化 task_context")
            if "manifest_sha256" in prompt or "source_sha256" in prompt or "run_id" in prompt:
                errors.append("Speaker Editor prompt 仍包含主流程应持有的编排元数据")
            if '"turn_id"' not in prompt or '"edited_text"' not in prompt:
                errors.append("Speaker Editor prompt 未声明极简 turn_id/edited_text 返回结构")
            for turn in task.get("task_context", {}).get("turns", []):
                if not isinstance(turn, dict):
                    continue
                source_turn_text = str(turn.get("text") or "")
                if prompt.count(source_turn_text) != 1:
                    errors.append("Speaker Editor prompt 未将每个 assigned turn 原文恰好呈现一次")
                speaker_label = str(turn.get("speaker_label") or "")
                if f"speaker={speaker_label}" not in prompt:
                    errors.append("Speaker Editor prompt 未清晰呈现 turn 的 speaker_label")
        if bundle.get("speaker_editing", {}).get("effective_mode") != "full":
            errors.append("显式 full 未进入并行 speaker editing")

        clean_manifest = build_speaker_turn_manifest(
            Path("2026-07-26 示例周会（已核对）.md"),
            "发言人1：我认为需求可能改善。\n发言人2：我们没有减仓，继续观察。",
            max_chars=100,
        )
        clean_bundle = build_mas_task_bundle_from_request(
            {
                "run_profile": "standard",
                "source_mode": "document_only",
                "meeting_type": "多人复盘会",
                "risk_flags": ["long_transcript", "filler_cleanup"],
                "materials": [{"kind": "document", "name": "2026-07-26 示例周会（已核对）.md"}],
                "speaker_turn_manifest": clean_manifest,
            }
        )
        errors.extend(validate_mas_task_bundle(clean_bundle))
        if clean_bundle.get("speaker_editing", {}).get("effective_mode") != "skip":
            errors.append("短 source 未被 auto 路由到主流程直接编辑")
        if any(
            isinstance(task, dict) and task.get("artifact_schema") == "speaker_turn_edit"
            for task in clean_bundle.get("tasks", [])
        ):
            errors.append("skip editing 仍生成 speaker edit task")
        if "editing_assembly_receipt" in clean_bundle.get("expected_artifacts", []):
            errors.append("skip editing 仍要求 editing_assembly_receipt")
        if clean_bundle.get("speaker_turn_manifest") != clean_manifest:
            errors.append("skip editing 未保留 speaker manifest 审计绑定")
        if DEFAULT_MAX_PARALLEL != 3:
            errors.append("MAS phase operator 默认并发未保持为 3 个 editor 槽")

        no_manifest_bundle = build_mas_task_bundle_from_request(
            {
                "run_profile": "standard",
                "source_mode": "document_only",
                "meeting_type": "多人复盘会",
                "risk_flags": ["long_transcript"],
                "materials": [{"kind": "document", "name": "direct-edit.txt"}],
            }
        )
        errors.extend(validate_mas_task_bundle(no_manifest_bundle))
        if no_manifest_bundle.get("speaker_editing", {}).get("effective_mode") != "not_applicable":
            errors.append("缺少 manifest 的 auto 路径未保留主流程直接编辑能力")
        if any(
            isinstance(task, dict) and task.get("artifact_schema") == "speaker_turn_edit"
            for task in no_manifest_bundle.get("tasks", [])
        ):
            errors.append("缺少 manifest 的主流程直接编辑路径错误生成 speaker task")

        long_paragraphs = [
            f"自然段{index}-" + chr(0x4E00 + index) * 6000
            for index in range(1, 4)
        ]
        long_source = "发言人1：" + "\n\n".join(long_paragraphs)
        long_manifest = build_speaker_turn_manifest(
            Path("speaker-editing-long-turn.txt"),
            long_source,
            max_chars=12000,
        )
        if long_manifest.get("shard_count") != 3:
            errors.append("超长单 turn 未按自然段拆成预期的三个工作包")
        if any(
            int(shard.get("char_count") or 0) > 12000
            for shard in long_manifest.get("shards", [])
            if isinstance(shard, dict)
        ):
            errors.append("超长单 turn 工作包超过 12000 字符目标上限")
        reconstructed = "".join(
            str(turn.get("text") or "").replace("\n", "")
            for turn in long_manifest.get("turns", [])
            if isinstance(turn, dict)
        )
        if reconstructed != "".join(long_paragraphs):
            errors.append("超长单 turn 分片未完整保持自然段内容")
        if any(
            sum(paragraph in str(turn.get("text") or "") for turn in long_manifest.get("turns", [])) != 1
            for paragraph in long_paragraphs
        ):
            errors.append("自然段被重复、遗漏或非必要地切断")
        long_bundle = build_mas_task_bundle_from_request(
            {
                "run_profile": "standard",
                "source_mode": "document_only",
                "meeting_type": "多人复盘会",
                "risk_flags": ["long_transcript"],
                "materials": [{"kind": "document", "name": "speaker-editing-long-turn.txt"}],
                "speaker_turn_manifest": long_manifest,
            }
        )
        errors.extend(validate_mas_task_bundle(long_bundle))
        if long_bundle.get("speaker_editing", {}).get("effective_mode") != "full":
            errors.append("超过 16000 字符的 source 未被 auto 路由为并行编辑")

        stress_text = "\n".join(
            f"发言人{speaker}：第{round_index}轮，我保留数字{speaker}和条件判断。"
            for round_index in range(1, 6)
            for speaker in range(1, 21)
        )
        stress_manifest = build_speaker_turn_manifest(
            Path("speaker-editing-stress.txt"),
            stress_text,
            max_chars=20,
        )
        stress_bundle = build_mas_task_bundle_from_request(
            {
                "run_profile": "standard",
                "source_mode": "document_only",
                "meeting_type": "多人复盘会",
                "risk_flags": ["long_transcript"],
                "materials": [{"kind": "document", "name": "speaker-editing-stress.txt"}],
                "speaker_turn_manifest": stress_manifest,
                "speaker_editing_mode": "full",
            }
        )
        errors.extend(validate_mas_task_bundle(stress_bundle))
        with tempfile.TemporaryDirectory(prefix="mas-speaker-stress-") as stress_tmpdir:
            stress_dispatch = write_mas_dispatch_files(stress_bundle, Path(stress_tmpdir))
            prompt_names = [Path(path).name for path in stress_dispatch["task_files"]]
            edit_prompt_names = [
                name for name in prompt_names if "speaker_turn_edit__" in name
            ]
            if len(edit_prompt_names) != 100 or len(edit_prompt_names) != len(set(edit_prompt_names)):
                errors.append("100-shard speaker editing 压力样例未生成唯一 prompt")
            if not any(name.startswith("100-") for name in prompt_names):
                errors.append("100+ task prompt 编号未覆盖三位数")
            stress_dispatch_manifest = json.loads(
                (Path(stress_tmpdir) / "dispatch_manifest.json").read_text(encoding="utf-8")
            )
            first_sixteen = [
                item
                for item in stress_dispatch_manifest.get("task_files", [])
                if isinstance(item, dict)
                and str(item.get("artifact_type") or "").startswith("speaker_turn_edit__")
            ][:16]
            batch_plan = plan_from_summary(
                {
                    "schema_version": "1.0",
                    "ok": False,
                    "task_dir": stress_tmpdir,
                    "next_action": {
                        "type": "collect_or_dispatch_phase_artifacts",
                        "phase": "editing",
                        "task_files": first_sixteen,
                    },
                    "warnings": [],
                },
                max_parallel=3,
            )
            batches = batch_plan.get("dispatch_batches", [])
            batched_types = [
                str(task.get("artifact_type") or "")
                for batch in batches
                for task in batch.get("tasks", [])
            ]
            expected_types = [str(item.get("artifact_type") or "") for item in first_sixteen]
            batch_sizes = [len(batch.get("tasks", [])) for batch in batches]
            if len(batches) != len(first_sixteen) or any(size != 1 for size in batch_sizes):
                errors.append("speaker editing 未做到一次 Agent 调用只处理一个 shard")
            if sorted(batched_types) != sorted(expected_types) or len(batched_types) != len(set(batched_types)):
                errors.append("speaker editing dispatch_batches 未恰好覆盖 task identity 一次")
            for task in batch_plan.get("dispatch_tasks", []):
                if not isinstance(task, dict):
                    continue
                command = str(task.get("ingest_command") or "")
                if "--speaker-task-id" not in command or str(task.get("task_id") or "") not in command:
                    errors.append("speaker editing ingest command 未携带可信 task_id")
            all_edit_files = [
                item
                for item in stress_dispatch_manifest.get("task_files", [])
                if isinstance(item, dict)
                and str(item.get("artifact_type") or "").startswith("speaker_turn_edit__")
            ]
            stress_batch_plan = plan_from_summary(
                {
                    "schema_version": "1.0",
                    "ok": False,
                    "task_dir": stress_tmpdir,
                    "next_action": {
                        "type": "collect_or_dispatch_phase_artifacts",
                        "phase": "editing",
                        "task_files": all_edit_files,
                    },
                    "warnings": [],
                },
                max_parallel=3,
            )
            stress_batches = stress_batch_plan.get("dispatch_batches", [])
            stress_waves = stress_batch_plan.get("dispatch_waves", [])
            if len(stress_batches) != 100 or any(
                len(batch.get("tasks", [])) != 1
                for batch in stress_batches
            ):
                errors.append("100 个 speaker tasks 未形成 100 个单-shard Agent 调用")
            if len(stress_waves) != 34 or any(
                len(wave.get("batch_ids", [])) > 3
                for wave in stress_waves
            ):
                errors.append("speaker editing dispatch_waves 未遵守 max_parallel=3")

        with tempfile.TemporaryDirectory(prefix="mas-speaker-editing-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            bound_bundle, dispatch_manifest = dispatch_context(task_dir)
            run_id = str(bound_bundle.get("run_id") or "")
            source_artifact = create_source_manifest(bound_bundle)[0]
            source_payload = {
                "run_id": run_id,
                "task_id": f"{run_id}:main:source_manifest",
                "dispatch_phase": "pre_draft",
                "artifact_owner": "Main Orchestrator",
                "artifact_type": "source_manifest",
                "artifact": source_artifact,
            }
            artifact_dir = task_dir / "artifacts"
            artifact_dir.mkdir()
            (artifact_dir / "source_manifest.json").write_text(
                json.dumps(source_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            baseline_entity_report = {
                "items": [],
                "local_candidate_paths": [],
                "external_evidence_paths": [],
                "confirmed_item_evidence_paths": {},
                "confirmed_items": [],
                "unresolved_items": [],
                "conflicts": [],
            }
            for artifact_type, artifact_value in (
                ("entity_verification_report", baseline_entity_report),
                ("doubtful_items", []),
            ):
                payload = fixture_payload(dispatch_manifest, artifact_type, artifact_value)
                (artifact_dir / f"{artifact_type}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            edit_bound_tasks = [
                task
                for task in bound_bundle.get("tasks", [])
                if isinstance(task, dict) and task.get("artifact_schema") == "speaker_turn_edit"
            ]
            sentinel = "当前仓位是两成"
            sentinel_task = next(
                (
                    task
                    for task in edit_bound_tasks
                    if sentinel in json.dumps(task.get("task_context"), ensure_ascii=False)
                ),
                None,
            )
            if not isinstance(sentinel_task, dict):
                errors.append("未找到 prompt source 唯一性测试 task")
            else:
                task_file = next(
                    (
                        item
                        for item in dispatch_manifest.get("task_files", [])
                        if isinstance(item, dict)
                        and item.get("artifact_type") == sentinel_task.get("artifact_type")
                    ),
                    None,
                )
                prompt_path = task_dir / str((task_file or {}).get("path") or "")
                prompt_text = (
                    prompt_path.read_text(encoding="utf-8")
                    if prompt_path.is_file()
                    else ""
                )
                if not prompt_path.is_file() or prompt_text.count(sentinel) != 1:
                    errors.append("Speaker Editor prompt 重复或遗漏 assigned source")
                if "Expected JSON Shape" in prompt_text or '"manifest_sha256"' in prompt_text:
                    errors.append("Speaker Editor task file 仍注入完整 artifact schema")

            invalid_ingest_checked = False
            structural_gates_checked = False
            duplicate_checked = False
            for edit_task_index, task in enumerate(edit_bound_tasks):
                source_turns = [
                    turn
                    for turn in task.get("task_context", {}).get("turns", [])
                    if isinstance(turn, dict)
                ]
                minimal_response = [
                    {
                        "turn_id": str(turn.get("turn_id") or ""),
                        "edited_text": str(turn.get("text") or ""),
                    }
                    for turn in source_turns
                ]
                payload = expand_speaker_edit_response(
                    minimal_response,
                    task_dir,
                    str(task.get("task_id") or ""),
                )
                if not invalid_ingest_checked:
                    try:
                        expand_speaker_edit_response(
                            minimal_response[:-1],
                            task_dir,
                            str(task.get("task_id") or ""),
                        )
                    except ValueError:
                        pass
                    else:
                        errors.append("缺 turn 的极简 speaker response 未在绑定前拒绝")
                    invalid_ingest_checked = True
                if not structural_gates_checked:
                    natural_edit_response = json.loads(
                        json.dumps(minimal_response, ensure_ascii=False)
                    )
                    natural_edit_response[0]["edited_text"] = (
                        str(natural_edit_response[0]["edited_text"])
                        .replace("嗯，那个，", "")
                        .replace("然后然后，", "然后，")
                    )
                    try:
                        expanded_natural_edit = expand_speaker_edit_response(
                            natural_edit_response,
                            task_dir,
                            str(task.get("task_id") or ""),
                        )
                    except ValueError as exc:
                        errors.append(f"符合 Skill 的自然编辑被极简 response 绑定误拒绝: {exc}")
                    else:
                        if expanded_natural_edit.get("task_id") != task.get("task_id"):
                            errors.append("极简 speaker response 未绑定正确 task_id")
                        if (
                            expanded_natural_edit.get("artifact", {})
                            .get("edited_turns", [{}])[0]
                            .get("source_sha256")
                            != source_turns[0].get("source_sha256")
                        ):
                            errors.append("主流程未从 task_context 补齐 speaker source_sha256")

                    hash_payload = json.loads(json.dumps(payload, ensure_ascii=False))
                    hash_payload["artifact"]["edited_turns"][0]["source_sha256"] = "0" * 64
                    hash_result = ingest_mas_artifact_module.ingest_mas_artifact(
                        hash_payload,
                        task_dir,
                        through_phase="editing",
                    )
                    if hash_result.get("ok"):
                        errors.append("speaker edit source_sha256 篡改未在 ingest 拒绝")

                    sequence_payload = json.loads(json.dumps(payload, ensure_ascii=False))
                    sequence_payload["artifact"]["edited_turns"][0]["sequence"] += 1
                    sequence_result = ingest_mas_artifact_module.ingest_mas_artifact(
                        sequence_payload,
                        task_dir,
                        through_phase="editing",
                    )
                    if sequence_result.get("ok"):
                        errors.append("speaker edit sequence 篡改未在 ingest 拒绝")

                    speaker_ids_payload = json.loads(json.dumps(payload, ensure_ascii=False))
                    speaker_ids_payload["artifact"]["speaker_ids"] = list(
                        reversed(speaker_ids_payload["artifact"]["speaker_ids"])
                    )
                    speaker_ids_result = ingest_mas_artifact_module.ingest_mas_artifact(
                        speaker_ids_payload,
                        task_dir,
                        through_phase="editing",
                    )
                    if speaker_ids_result.get("ok"):
                        errors.append("speaker edit speaker_ids 顺序篡改未在 ingest 拒绝")

                    foreign_payload = json.loads(json.dumps(payload, ensure_ascii=False))
                    foreign_payload["artifact_type"] = "speaker_turn_edit__foreign__part_01"
                    foreign_result = ingest_mas_artifact_module.ingest_mas_artifact(
                        foreign_payload,
                        task_dir,
                        through_phase="editing",
                    )
                    if foreign_result.get("ok"):
                        errors.append("foreign speaker artifact 未在 ingest 拒绝")
                    structural_gates_checked = True
                artifact = payload.get("artifact", {})
                for turn in artifact.get("edited_turns", []):
                    if isinstance(turn, dict):
                        edited = str(turn.get("edited_text") or "")
                        for filler in ("嗯，那个，", "呃，", "然后然后，"):
                            edited = edited.replace(filler, "")
                        edited = edited.replace("不不，", "不，").replace("没有没有，", "没有，")
                        turn["edited_text"] = edited
                minimal_response = [
                    {
                        "turn_id": str(turn.get("turn_id") or ""),
                        "edited_text": str(turn.get("edited_text") or ""),
                    }
                    for turn in artifact.get("edited_turns", [])
                    if isinstance(turn, dict)
                ]
                if edit_task_index == 0:
                    minimal_return_path = task_dir / "speaker-editor-return.json"
                    minimal_return_path.write_text(
                        json.dumps(minimal_response, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    ingest_result = ingest_mas_artifact_file(
                        minimal_return_path,
                        task_dir,
                        through_phase="editing",
                        speaker_task_id=str(task.get("task_id") or ""),
                    )
                else:
                    ingest_result = ingest_mas_artifact_module.ingest_mas_artifact(
                        payload,
                        task_dir,
                        through_phase="editing",
                    )
                if not ingest_result.get("ok"):
                    errors.append(
                        "speaker edit artifact ingest 失败: "
                        + "; ".join(str(item) for item in ingest_result.get("errors", []))
                    )
                elif not duplicate_checked:
                    duplicate_result = ingest_mas_artifact_module.ingest_mas_artifact(
                        payload,
                        task_dir,
                        through_phase="editing",
                    )
                    if duplicate_result.get("ok"):
                        errors.append("duplicate speaker artifact 未在 ingest 拒绝")
                    duplicate_checked = True
                if edit_task_index == len(edit_bound_tasks) - 2:
                    missing_summary = collect_mas_run(task_dir, through_phase="editing")
                    if missing_summary.get("next_action", {}).get("type") == "assemble_edited_turns_before_draft_review":
                        errors.append("缺少最后一个 shard 时 collector 错误进入组装")

            before_assembly = collect_mas_run(task_dir, through_phase="editing")
            if before_assembly.get("next_action", {}).get("type") != "assemble_edited_turns_before_draft_review":
                errors.append("全部 speaker edit 到齐后未进入主流程组装 gate")
            try:
                assembly = assemble_speaker_turn_edits(task_dir)
            except Exception as exc:
                errors.append(f"speaker edit assembly 失败: {exc}")
                assembly = {}
            after_assembly = collect_mas_run(task_dir, through_phase="editing")
            if not after_assembly.get("ok"):
                errors.append(
                    "speaker edit assembly receipt 未通过 collector: "
                    + "; ".join(str(item) for item in after_assembly.get("errors", []))
                )
            working_path = Path(str(assembly.get("working_draft") or ""))
            if working_path.is_file():
                working_text = working_path.read_text(encoding="utf-8")
                for forbidden in ("嗯，那个，", "呃，", "然后然后，", "不不，", "没有没有，"):
                    if forbidden in working_text:
                        errors.append(f"speaker edit 工作稿仍保留目标 filler: {forbidden}")
                preserved = (
                    "我觉得需求还是比较强",
                    "我们没有减仓",
                    "当前仓位是两成",
                    "不，我不是说需求下滑，而是增速可能放缓",
                    "我不会加仓",
                    "没有，需求并没有下滑，暂时只是交付推迟",
                )
                if any(term not in working_text for term in preserved):
                    errors.append("speaker edit 工作稿丢失第一人称、否定、数字或条件")
                positions = [working_text.find(term) for term in preserved]
                if positions != sorted(positions) or any(position < 0 for position in positions):
                    errors.append("speaker edit 工作稿未按全局 sequence 组装")
            else:
                errors.append("speaker edit assembly 未生成工作稿")

            first_edit_path = artifact_dir / f"{edit_types[0]}.json"
            if first_edit_path.is_file():
                replaced = json.loads(first_edit_path.read_text(encoding="utf-8"))
                replaced["artifact"]["edited_turns"][0]["edited_text"] += "。"
                first_edit_path.write_text(
                    json.dumps(replaced, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                stale_summary = collect_mas_run(task_dir, through_phase="editing")
                if stale_summary.get("ok") or not any(
                    "edit_artifact_digest" in str(item)
                    for item in stale_summary.get("errors", [])
                ):
                    errors.append("speaker edit artifact 变化后旧 assembly receipt 未失效")

            forbidden_payload = json.loads(
                json.dumps(
                    next(
                        task.get("expected_output_shape")
                        for task in bound_bundle.get("tasks", [])
                        if isinstance(task, dict) and task.get("artifact_schema") == "speaker_turn_edit"
                    ),
                    ensure_ascii=False,
                )
            )
            forbidden_payload["artifact"]["final_markdown"] = "# forbidden"
            forbidden_result = validate_mas_artifacts_payload(forbidden_payload)
            if forbidden_result.get("ok"):
                errors.append("speaker edit artifact 携带 final_markdown 未被拒绝")

        ordered_manifest = build_speaker_turn_manifest(
            Path("speaker-editing-ordered-turns.txt"),
            "发言人1：第一轮保留原始顺序。\n发言人1：第二轮继续补充条件。",
            max_chars=8000,
        )
        ordered_bundle = build_mas_task_bundle_from_request(
            {
                "run_profile": "standard",
                "source_mode": "document_only",
                "meeting_type": "多人复盘会",
                "risk_flags": ["speaker_turn_editing"],
                "materials": [{"kind": "document", "name": "speaker-editing-ordered-turns.txt"}],
                "speaker_turn_manifest": ordered_manifest,
                "speaker_editing_mode": "full",
            }
        )
        with tempfile.TemporaryDirectory(prefix="mas-speaker-order-") as order_tmpdir:
            order_task_dir = Path(order_tmpdir)
            write_mas_dispatch_files(ordered_bundle, order_task_dir)
            ordered_bound_bundle, _ = dispatch_context(order_task_dir)
            ordered_task = next(
                task
                for task in ordered_bound_bundle.get("tasks", [])
                if isinstance(task, dict) and task.get("artifact_schema") == "speaker_turn_edit"
            )
            ordered_payload = json.loads(
                json.dumps(ordered_task.get("expected_output_shape"), ensure_ascii=False)
            )
            ordered_sources = {
                str(turn.get("turn_id") or ""): str(turn.get("text") or "")
                for turn in ordered_task.get("task_context", {}).get("turns", [])
                if isinstance(turn, dict)
            }
            for returned_turn in ordered_payload.get("artifact", {}).get("edited_turns", []):
                if isinstance(returned_turn, dict):
                    returned_turn["edited_text"] = ordered_sources.get(
                        str(returned_turn.get("turn_id") or ""),
                        "",
                    )
            ordered_payload["artifact"]["edited_turns"].reverse()
            ordered_result = ingest_mas_artifact_module.ingest_mas_artifact(
                ordered_payload,
                order_task_dir,
                through_phase="editing",
            )
            if ordered_result.get("ok") or not any(
                "顺序" in str(item)
                for item in ordered_result.get("errors", [])
            ):
                errors.append("speaker edit 返回 turn 顺序变化未被拒绝")

        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "turn_count": speaker_manifest.get("turn_count"),
            "shard_count": speaker_manifest.get("shard_count"),
        }
    elif case.get("check") == "mas_task_bundle":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors = validate_mas_task_bundle(bundle)
        warnings: list[str] = []
        expected_artifacts = [str(item) for item in bundle.get("expected_artifacts", [])]
        artifact_owners = {
            str(key): str(value)
            for key, value in dict(bundle.get("artifact_owners", {})).items()
        }
        roles = [str(task.get("role") or "") for task in bundle.get("tasks", []) if isinstance(task, dict)]
        bundle_text = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
        if "expect_mas_required" in case and bool(bundle.get("mas_required")) != bool(case["expect_mas_required"]):
            errors.append(
                f"MAS task bundle mas_required 不符合预期: expected={bool(case['expect_mas_required'])} "
                f"actual={bool(bundle.get('mas_required'))}"
            )
        for artifact in [str(item) for item in case.get("require_artifacts", [])]:
            if artifact not in expected_artifacts:
                errors.append(f"MAS task bundle 缺少 expected_artifact: {artifact}")
        for artifact in [str(item) for item in case.get("forbid_artifacts", [])]:
            if artifact in expected_artifacts:
                errors.append(f"MAS task bundle 不应包含 expected_artifact: {artifact}")
        for role in [str(item) for item in case.get("require_roles", [])]:
            if role not in roles:
                errors.append(f"MAS task bundle 缺少 role: {role}")
        tasks_by_artifact = {
            str(task.get("artifact_type") or ""): task
            for task in bundle.get("tasks", [])
            if isinstance(task, dict)
        }
        for artifact_type, required_inputs in dict(case.get("require_task_inputs", {})).items():
            task = tasks_by_artifact.get(str(artifact_type))
            if task is None:
                errors.append(f"MAS task bundle 缺少用于检查 inputs 的 task: {artifact_type}")
                continue
            actual_inputs = {str(item) for item in task.get("inputs", [])}
            for required_input in [str(item) for item in required_inputs]:
                if required_input not in actual_inputs:
                    errors.append(
                        f"MAS task bundle {artifact_type} inputs 缺少: {required_input}"
                    )
        for artifact, owner in dict(case.get("require_artifact_owners", {})).items():
            if artifact_owners.get(str(artifact)) != str(owner):
                errors.append(
                    f"MAS task bundle artifact owner 不符合预期: {artifact} "
                    f"expected={owner} actual={artifact_owners.get(str(artifact))}"
                )
        for term in [str(term) for term in case.get("required_terms", [])]:
            if term not in bundle_text:
                errors.append(f"MAS task bundle 缺少文本锚点: {term}")
        for term in [str(term) for term in case.get("forbidden_terms", [])]:
            if term in bundle_text:
                errors.append(f"MAS task bundle 包含禁止锚点: {term}")
        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "mas_required": bool(bundle.get("mas_required")),
            "expected_artifacts": expected_artifacts,
            "artifact_owners": artifact_owners,
            "roles": roles,
        }
    elif case.get("check") == "mas_task_bundle_reject":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        errors = []
        try:
            bundle = build_mas_task_bundle_from_request(request_payload)
            validation_errors = validate_mas_task_bundle(bundle)
            if validation_errors:
                raise ValueError("; ".join(validation_errors))
        except ValueError as exc:
            errors.append(str(exc))
        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": [],
        }
    elif case.get("check") == "mas_task_bundle_cli":
        command = [
            sys.executable,
            str(SCRIPT_DIR / "build_mas_task_bundle.py"),
            *[str(item) for item in case.get("cli_args", [])],
        ]
        expected_returncode = int(case.get("expect_returncode", 0))
        with tempfile.TemporaryDirectory(prefix="mas-task-bundle-cli-") as tmpdir:
            if case.get("with_task_dir"):
                command.extend(["--task-dir", tmpdir])
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=utf8_subprocess_env(),
                timeout=20,
                check=False,
            )
            errors = []
            if completed.returncode != expected_returncode:
                errors.append(
                    "MAS task bundle CLI returncode 不符合预期: "
                    f"expected={expected_returncode} actual={completed.returncode}"
                )
            output_text = completed.stdout + completed.stderr
            for term in [str(item) for item in case.get("required_terms", [])]:
                if term not in output_text:
                    errors.append(f"MAS task bundle CLI 缺少文本锚点: {term}")
            for filename in [str(item) for item in case.get("require_task_files", [])]:
                if not (Path(tmpdir) / filename).is_file():
                    errors.append(f"MAS task bundle CLI 缺少派发文件: {filename}")
            if case.get("check_repeat_requires_overwrite") and completed.returncode == 0:
                repeated = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=utf8_subprocess_env(),
                    timeout=20,
                    check=False,
                )
                repeated_text = repeated.stdout + repeated.stderr
                if repeated.returncode != 1 or "already contains dispatch files" not in repeated_text:
                    errors.append("MAS task bundle CLI 重复派发未要求显式覆盖授权")
                overwritten = subprocess.run(
                    [*command, "--overwrite-dispatch"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    env=utf8_subprocess_env(),
                    timeout=20,
                    check=False,
                )
                if overwritten.returncode != 0:
                    errors.append(
                        "MAS task bundle CLI 显式覆盖派发失败: "
                        + overwritten.stdout
                        + overwritten.stderr
                    )
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": [],
                "returncode": completed.returncode,
            }
    elif case.get("check") == "mas_task_bundle_mutation_reject":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        bundle = build_mas_task_bundle_from_request(request_payload)
        mutation = str(case.get("mutation") or "")
        if mutation == "audio_profile":
            bundle["run_profile"] = "standard"
        elif mutation == "drop_entity_secondary":
            for task in bundle.get("tasks", []):
                if isinstance(task, dict) and task.get("artifact_type") == "entity_verification_report":
                    task["secondary_artifacts"] = []
                    task.pop("secondary_required_fields", None)
                    break
        elif mutation == "drop_entity_scope":
            removed = {"entity_verification_report", "doubtful_items"}
            bundle["expected_artifacts"] = [
                item for item in bundle.get("expected_artifacts", []) if str(item) not in removed
            ]
            bundle["tasks"] = [
                task
                for task in bundle.get("tasks", [])
                if not isinstance(task, dict) or str(task.get("artifact_type") or "") != "entity_verification_report"
            ]
            for artifact_type in removed:
                bundle.get("artifact_owners", {}).pop(artifact_type, None)
            if isinstance(bundle.get("validation"), dict):
                bundle["validation"]["required_artifacts"] = list(bundle["expected_artifacts"])
        elif mutation == "duplicate_task":
            entity_task = next(
                task
                for task in bundle.get("tasks", [])
                if isinstance(task, dict) and task.get("artifact_type") == "entity_verification_report"
            )
            bundle["tasks"].append(json.loads(json.dumps(entity_task, ensure_ascii=False)))
        elif mutation == "duplicate_expected_artifact":
            bundle["expected_artifacts"].append(bundle["expected_artifacts"][0])
        else:
            raise ValueError(f"未知 MAS bundle mutation: {mutation}")
        mutation_errors = validate_mas_task_bundle(bundle)
        result = {
            "ok": not mutation_errors,
            "errors": mutation_errors,
            "warnings": [],
        }
    elif case.get("check") == "mas_task_dispatch_files":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors = validate_mas_task_bundle(bundle)
        warnings: list[str] = []
        with tempfile.TemporaryDirectory(prefix="mas-task-dispatch-") as tmpdir:
            dispatch_result = write_mas_dispatch_files(bundle, Path(tmpdir))
            manifest_path = Path(dispatch_result["manifest_file"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            bound_bundle = json.loads(Path(dispatch_result["bundle_file"]).read_text(encoding="utf-8"))
            task_files = [Path(path) for path in dispatch_result["task_files"]]
            if manifest.get("schema_version") != "1.0":
                errors.append(f"dispatch_manifest schema_version 不符合预期: {manifest.get('schema_version')}")
            bundle_file = Path(tmpdir) / str(manifest.get("bundle_file") or "")
            if not bundle_file.exists():
                errors.append(f"dispatch_manifest bundle_file 不存在: {manifest.get('bundle_file')}")
            if int(manifest.get("task_count", -1)) != len(task_files):
                errors.append(
                    f"dispatch_manifest task_count 不符合实际文件数: {manifest.get('task_count')} != {len(task_files)}"
                )
            manifest_task_files = manifest.get("task_files")
            if not isinstance(manifest_task_files, list):
                errors.append("dispatch_manifest task_files 必须是 JSON array")
                manifest_task_files = []
            manifest_paths = []
            for item in manifest_task_files:
                if not isinstance(item, dict):
                    errors.append("dispatch_manifest task_files item 必须是 JSON object")
                    continue
                path_name = str(item.get("path") or "")
                manifest_paths.append(path_name)
                if not (Path(tmpdir) / path_name).exists():
                    errors.append(f"dispatch_manifest task file path 不存在: {path_name}")
                if str(item.get("dispatch_phase") or "") not in {"pre_draft", "editing", "draft_review", "final_verification"}:
                    errors.append(f"dispatch_manifest task dispatch_phase 不合法: {item.get('dispatch_phase')}")
            actual_names = [path.name for path in task_files]
            if manifest_paths != actual_names:
                errors.append(f"dispatch_manifest task_files 顺序不符合实际生成文件: {manifest_paths} != {actual_names}")
            for filename in [str(item) for item in case.get("require_task_files", [])]:
                if not (Path(tmpdir) / filename).exists():
                    errors.append(f"缺少 MAS dispatch task file: {filename}")
            combined_task_text = "\n".join(path.read_text(encoding="utf-8") for path in task_files)
            manifest_text = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
            for task in bound_bundle.get("tasks", []):
                if not isinstance(task, dict):
                    continue
                shape_result = validate_mas_artifacts_payload(task.get("expected_output_shape"))
                if not shape_result.get("ok"):
                    errors.append(
                        f"MAS task expected_output_shape 无法通过自身 schema: {task.get('artifact_type')}: "
                        + "; ".join(str(item) for item in shape_result.get("errors", []))
                    )
            expected_primary = str(case.get("expect_source_reconciliation_primary") or "")
            if expected_primary:
                source_tasks = [
                    task
                    for task in bound_bundle.get("tasks", [])
                    if isinstance(task, dict) and task.get("artifact_type") == "source_reconciliation"
                ]
                actual_primary = ""
                if len(source_tasks) == 1:
                    shape = source_tasks[0].get("expected_output_shape")
                    if isinstance(shape, dict):
                        artifact = shape.get("artifact")
                        if isinstance(artifact, dict):
                            actual_primary = str(artifact.get("primary_body_source") or "")
                if actual_primary != expected_primary:
                    errors.append(
                        "MAS source_reconciliation expected_output_shape 主源不符合 source_mode: "
                        f"expected={expected_primary} actual={actual_primary}"
                    )
            for term in [str(term) for term in case.get("required_terms", [])]:
                if term not in combined_task_text and term not in manifest_text:
                    errors.append(f"MAS dispatch files 缺少文本锚点: {term}")
            for term in [str(term) for term in case.get("forbidden_terms", [])]:
                if term in combined_task_text or term in manifest_text:
                    errors.append(f"MAS dispatch files 包含禁止锚点: {term}")
            if case.get("check_overwrite_prompt_cleanup"):
                stale_prompt = Path(tmpdir) / "99-stale.prompt.md"
                stale_prompt.write_text("stale prompt\n", encoding="utf-8")
                write_mas_dispatch_files(bundle, Path(tmpdir), overwrite_prompts=True)
                if stale_prompt.exists():
                    errors.append("MAS dispatch 显式覆盖后未清理旧 prompt")
                dispatch_before_reject = {
                    path.name: path.read_bytes()
                    for path in Path(tmpdir).glob("*")
                    if path.is_file() and (
                        path.name in {"mas_task_bundle.json", "dispatch_manifest.json"}
                        or path.name.endswith(".prompt.md")
                    )
                }
                try:
                    write_mas_dispatch_files(bundle, Path(tmpdir), overwrite_prompts=False)
                except ValueError as exc:
                    if "already contains dispatch files" not in str(exc):
                        errors.append(f"MAS dispatch 非覆盖模式错误不符合预期: {exc}")
                else:
                    errors.append("MAS dispatch 非覆盖模式未拒绝既有派发目录")
                dispatch_after_reject = {
                    path.name: path.read_bytes()
                    for path in Path(tmpdir).glob("*")
                    if path.is_file() and (
                        path.name in {"mas_task_bundle.json", "dispatch_manifest.json"}
                        or path.name.endswith(".prompt.md")
                    )
                }
                if dispatch_after_reject != dispatch_before_reject:
                    errors.append("MAS dispatch 非覆盖模式拒绝时仍改写了派发文件")
            result = {
                "ok": not errors,
                "errors": errors,
                "warnings": warnings,
                "task_count": len(task_files),
                "task_file_names": [path.name for path in task_files],
            }
    elif case.get("check") == "mas_collector_corrupt_control":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        with tempfile.TemporaryDirectory(prefix="mas-corrupt-control-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            observed: list[dict[str, Any]] = []
            (task_dir / "mas_task_bundle.json").write_text("{invalid-json\n", encoding="utf-8")
            corrupt_bundle = collect_mas_run(task_dir)
            bundle_error_text = "\n".join(str(item) for item in corrupt_bundle.get("errors", []))
            if corrupt_bundle.get("ok") or "无法读取 MAS task bundle" not in bundle_error_text:
                result = {
                    "ok": False,
                    "errors": ["MAS collector 未将损坏 bundle 转成结构化失败"],
                    "warnings": corrupt_bundle.get("warnings", []),
                }
            else:
                observed.append({"case": "corrupt_bundle", "errors": corrupt_bundle.get("errors", [])})
                for path in (task_dir / "artifacts").glob("*.json"):
                    path.unlink()
                write_mas_dispatch_files(bundle, task_dir, overwrite_prompts=True)

                manifest_path = task_dir / "dispatch_manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["task_count"] = int(manifest.get("task_count", 0)) + 1
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                corrupt_manifest = collect_mas_run(task_dir)
                manifest_error_text = "\n".join(str(item) for item in corrupt_manifest.get("errors", []))
                if corrupt_manifest.get("ok") or "task_count" not in manifest_error_text:
                    result = {
                        "ok": False,
                        "errors": ["MAS collector 未拦截 task_count 与 task_files 不一致"],
                        "warnings": corrupt_manifest.get("warnings", []),
                    }
                else:
                    observed.append({"case": "corrupt_manifest_task_count", "errors": corrupt_manifest.get("errors", [])})
                    for path in (task_dir / "artifacts").glob("*.json"):
                        path.unlink()
                    write_mas_dispatch_files(bundle, task_dir, overwrite_prompts=True)
                    _, dispatch_manifest = dispatch_context(task_dir)
                    (task_dir / "artifacts").mkdir(parents=True, exist_ok=True)
                    fixture_payload_data = json.loads(
                        (base_dir / "mas_artifacts_valid.json").read_text(encoding="utf-8")
                    )
                    source_manifest = fixture_payload_data["artifacts"]["source_manifest"]
                    reserved_payload = fixture_payload(dispatch_manifest, "source_manifest", source_manifest)
                    reserved_payload["task_artifact_set"] = ["source_manifest"]
                    reserved_payload["ingested_split"] = True
                    (task_dir / "artifacts" / "source_manifest.json").write_text(
                        json.dumps(reserved_payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    reserved_result = collect_mas_run(task_dir, through_phase="pre_draft")
                    reserved_error_text = "\n".join(str(item) for item in reserved_result.get("errors", []))
                    if reserved_result.get("ok") or "内部拆分字段" not in reserved_error_text:
                        result = {
                            "ok": False,
                            "errors": ["MAS collector 未拒绝直接落盘的内部拆分字段"],
                            "warnings": reserved_result.get("warnings", []),
                        }
                    else:
                        observed.append({"case": "reserved_split_direct_collection", "errors": reserved_result.get("errors", [])})
                        result = {
                            "ok": True,
                            "errors": [],
                            "warnings": [],
                            "observed_structured_errors": observed,
                        }
    elif case.get("check") == "mas_collect_artifacts":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        artifact_fixture_path = base_dir / str(case["artifact_file"])
        artifact_payload = json.loads(artifact_fixture_path.read_text(encoding="utf-8"))
        fixture_artifacts = artifact_payload.get("artifacts")
        if not isinstance(fixture_artifacts, dict):
            raise ValueError(f"MAS artifact fixture 必须包含 artifacts object: {artifact_fixture_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors = validate_mas_task_bundle(bundle)
        warnings: list[str] = []
        omitted = {str(item) for item in case.get("omit_artifacts", [])}
        duplicated = {str(item) for item in case.get("duplicate_artifacts", [])}
        with tempfile.TemporaryDirectory(prefix="mas-collect-artifacts-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            _, dispatch_manifest = dispatch_context(task_dir)
            synthetic_markdown = task_dir / "synthetic-final.md"
            synthetic_markdown.write_text(synthetic_final_markdown(fixture_artifacts), encoding="utf-8")
            if case.get("invalid_markdown_claimed_valid"):
                synthetic_markdown.write_text("# fake validator pass\n", encoding="utf-8")
            verification_payload = synthetic_verification_payload(fixture_artifacts)
            tamper_sidecar_field = case.get("tamper_sidecar_field")
            if isinstance(tamper_sidecar_field, dict):
                records = verification_payload.get("records")
                if isinstance(records, list) and records and isinstance(records[0], dict):
                    records[0][str(tamper_sidecar_field.get("field") or "")] = tamper_sidecar_field.get("value")
            (task_dir / "synthetic.verification.json").write_text(
                json.dumps(verification_payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            artifact_dir = task_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            deferred_export: tuple[str, Any] | None = None
            for artifact_type, artifact in fixture_artifacts.items():
                artifact_type = str(artifact_type)
                if artifact_type in omitted or artifact_type not in bundle.get("expected_artifacts", []):
                    continue
                if artifact_type == "source_manifest" and case.get("source_manifest_first_material_name"):
                    artifact = json.loads(json.dumps(artifact, ensure_ascii=False))
                    materials = artifact.get("materials") if isinstance(artifact, dict) else None
                    if isinstance(materials, list) and materials and isinstance(materials[0], dict):
                        materials[0]["name"] = str(case["source_manifest_first_material_name"])
                if artifact_type == "source_manifest" and case.get("use_generated_source_manifest"):
                    artifact, _ = create_source_manifest(request_payload, archive_allowed=False)
                if artifact_type == "source_reconciliation" and (
                    "source_reconciliation_primary" in case or "source_reconciliation_cross_check" in case
                ):
                    artifact = json.loads(json.dumps(artifact, ensure_ascii=False))
                    if "source_reconciliation_primary" in case:
                        artifact["primary_body_source"] = case.get("source_reconciliation_primary")
                    if "source_reconciliation_cross_check" in case:
                        artifact["cross_check_source"] = case.get("source_reconciliation_cross_check")
                if case.get("record_main_actions") and artifact_type == "export_manifest":
                    deferred_export = (artifact_type, artifact)
                    continue
                payload = fixture_payload(dispatch_manifest, artifact_type, artifact, synthetic_markdown)
                if artifact_type == str(case.get("top_level_final_field_artifact") or ""):
                    payload["final_markdown"] = "# forbidden direct collector field"
                (artifact_dir / f"{artifact_type}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                if artifact_type in duplicated:
                    (artifact_dir / f"duplicate-{artifact_type}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            if case.get("tamper_markdown_after_reviews"):
                synthetic_markdown.write_text(
                    synthetic_markdown.read_text(encoding="utf-8") + "review-stale\n",
                    encoding="utf-8",
                )
            if case.get("record_main_actions"):
                result = collect_mas_run(task_dir, through_phase="draft_review")
                summary_path = task_dir / "mas_run_summary.json"
                summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                record_main_actions(task_dir, synthetic_markdown, summary_path=summary_path)
                if deferred_export:
                    artifact_type, artifact = deferred_export
                    payload = fixture_payload(dispatch_manifest, artifact_type, artifact, synthetic_markdown)
                    (artifact_dir / f"{artifact_type}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                if case.get("tamper_markdown_after_receipt"):
                    synthetic_markdown.write_text(
                        synthetic_markdown.read_text(encoding="utf-8") + "tampered after verification\n",
                        encoding="utf-8",
                    )
            result = collect_mas_run(
                task_dir,
                through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
            )
            result["errors"] = errors + result["errors"]
            result["warnings"] = warnings + result["warnings"]
            result["ok"] = not result["errors"] and bool(result["ok"])
        expected_decision = case.get("expect_decision")
        if expected_decision and result.get("decision", {}).get("decision") != expected_decision:
            result["errors"].append(
                f"MAS collected decision 不符合预期: expected={expected_decision} "
                f"actual={result.get('decision', {}).get('decision')}"
            )
            result["ok"] = False
        for artifact in [str(item) for item in case.get("require_artifacts", [])]:
            if artifact not in result.get("artifact_types", []):
                result["errors"].append(f"MAS collected artifacts 缺少 artifact: {artifact}")
                result["ok"] = False
        expected_next_action = case.get("expect_next_action_type")
        if expected_next_action and result.get("next_action", {}).get("type") != expected_next_action:
            result["errors"].append(
                f"MAS next_action 不符合预期: expected={expected_next_action} "
                f"actual={result.get('next_action', {}).get('type')}"
            )
            result["ok"] = False
        expected_next_phase = case.get("expect_next_phase")
        if expected_next_phase and result.get("next_action", {}).get("phase") != expected_next_phase:
            result["errors"].append(
                f"MAS next_action phase 不符合预期: expected={expected_next_phase} "
                f"actual={result.get('next_action', {}).get('phase')}"
            )
            result["ok"] = False
        next_action_text = json.dumps(result.get("next_action", {}), ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("require_next_action_terms", [])]:
            if term not in next_action_text:
                result["errors"].append(f"MAS next_action 缺少文本锚点: {term}")
                result["ok"] = False
        duplicate_types = {
            str(item.get("artifact_type") or "")
            for item in result.get("duplicate_artifacts", [])
            if isinstance(item, dict)
        }
        for artifact_type in [str(item) for item in case.get("require_duplicate_artifacts", [])]:
            if artifact_type not in duplicate_types:
                result["errors"].append(f"MAS duplicate_artifacts 缺少 artifact: {artifact_type}")
                result["ok"] = False
    elif case.get("check") == "mas_dry_run":
        artifact_fixture_path = base_dir / str(case["artifact_file"])
        with tempfile.TemporaryDirectory(prefix="mas-dry-run-") as tmpdir:
            result = run_mas_dry_run(file_path, artifact_fixture_path, Path(tmpdir))
            combined_payload = json.loads(
                Path(str(result.get("combined_artifacts_file") or "")).read_text(encoding="utf-8")
            )
            combined_artifacts = combined_payload.get("artifacts", {})
            result["combined_artifact_types"] = sorted(combined_artifacts) if isinstance(combined_artifacts, dict) else []
            if isinstance(combined_artifacts, dict):
                export_manifest = combined_artifacts.get("export_manifest", {})
                markdown_path = Path(str(export_manifest.get("markdown_path") or "")) if isinstance(export_manifest, dict) else Path()
                result["combined_export_hash_matches"] = bool(
                    isinstance(export_manifest, dict)
                    and markdown_path.is_file()
                    and export_manifest.get("markdown_sha256") == file_sha256(markdown_path)
                )
        expected_phase_order = [str(item) for item in case.get("expect_phase_order", [])]
        if expected_phase_order and result.get("phase_order") != expected_phase_order:
            result["errors"].append(
                f"MAS dry-run phase_order 不符合预期: expected={expected_phase_order} "
                f"actual={result.get('phase_order')}"
            )
            result["ok"] = False
        expected_completed_phase_order = [str(item) for item in case.get("expect_completed_phase_order", [])]
        if expected_completed_phase_order and result.get("completed_phase_order") != expected_completed_phase_order:
            result["errors"].append(
                f"MAS dry-run completed_phase_order 不符合预期: expected={expected_completed_phase_order} "
                f"actual={result.get('completed_phase_order')}"
            )
            result["ok"] = False
        expected_stop_reason = case.get("expect_stop_reason")
        if expected_stop_reason and result.get("stop_reason") != expected_stop_reason:
            result["errors"].append(
                f"MAS dry-run stop_reason 不符合预期: expected={expected_stop_reason} "
                f"actual={result.get('stop_reason')}"
            )
            result["ok"] = False
        phase_results = {
            str(item.get("phase") or ""): item
            for item in result.get("phases", [])
            if isinstance(item, dict)
        }
        for expected_phase_action in case.get("expect_phase_next_actions", []):
            if not isinstance(expected_phase_action, dict):
                result["errors"].append("MAS dry-run expect_phase_next_actions item 必须是 JSON object")
                result["ok"] = False
                continue
            phase = str(expected_phase_action.get("phase") or "")
            phase_result = phase_results.get(phase)
            if not phase_result:
                result["errors"].append(f"MAS dry-run 缺少 phase 结果: {phase}")
                result["ok"] = False
                continue
            next_action = phase_result.get("next_action", {})
            expected_type = expected_phase_action.get("type")
            if expected_type and next_action.get("type") != expected_type:
                result["errors"].append(
                    f"MAS dry-run {phase} next_action 不符合预期: expected={expected_type} "
                    f"actual={next_action.get('type')}"
                )
                result["ok"] = False
            expected_phase = expected_phase_action.get("next_phase")
            if expected_phase and next_action.get("phase") != expected_phase:
                result["errors"].append(
                    f"MAS dry-run {phase} next_action phase 不符合预期: expected={expected_phase} "
                    f"actual={next_action.get('phase')}"
                )
                result["ok"] = False
        expected_next_action = case.get("expect_final_next_action_type")
        if expected_next_action and result.get("final_next_action", {}).get("type") != expected_next_action:
            result["errors"].append(
                f"MAS dry-run final_next_action 不符合预期: expected={expected_next_action} "
                f"actual={result.get('final_next_action', {}).get('type')}"
            )
            result["ok"] = False
        expected_next_phase = case.get("expect_final_next_phase")
        if expected_next_phase and result.get("final_next_action", {}).get("phase") != expected_next_phase:
            result["errors"].append(
                f"MAS dry-run final_next_action phase 不符合预期: expected={expected_next_phase} "
                f"actual={result.get('final_next_action', {}).get('phase')}"
            )
            result["ok"] = False
        for artifact_type in [str(item) for item in case.get("expect_combined_artifacts", [])]:
            if artifact_type not in result.get("combined_artifact_types", []):
                result["errors"].append(f"MAS dry-run combined artifacts 缺少: {artifact_type}")
                result["ok"] = False
        if case.get("expect_combined_export_hash_matches") and not result.get("combined_export_hash_matches"):
            result["errors"].append("MAS dry-run combined export_manifest 哈希未绑定实际 Markdown")
            result["ok"] = False
        trace_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("require_trace_terms", [])]:
            if term not in trace_text:
                result["errors"].append(f"MAS dry-run trace 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_ingest_artifact":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors = validate_mas_task_bundle(bundle)
        warnings: list[str] = []
        artifact_input_path = base_dir / str(case["artifact_file"])
        with tempfile.TemporaryDirectory(prefix="mas-ingest-artifact-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            _, dispatch_manifest = dispatch_context(task_dir)
            bound_return_path = bind_fixture_return(
                artifact_input_path,
                task_dir / "returned-artifact.json",
                dispatch_manifest,
            )
            result = ingest_mas_artifact_file(
                bound_return_path,
                task_dir,
                through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
            )
            result["errors"] = errors + result["errors"]
            result["warnings"] = warnings + result["warnings"]
            result["ok"] = not result["errors"] and bool(result["ok"])
            written_types = {
                str(item.get("artifact_type") or "")
                for item in result.get("written_artifacts", [])
                if isinstance(item, dict)
            }
            for artifact in [str(item) for item in case.get("expect_written_artifacts", [])]:
                if artifact not in written_types:
                    result["errors"].append(f"MAS ingest 缺少写入 artifact: {artifact}")
                    result["ok"] = False
            if case.get("expect_repair_history") and not result.get("repair_history_file"):
                result["errors"].append("MAS ingest 未写入 repair_history_file")
                result["ok"] = False
            if case.get("expect_next_collector_term"):
                term = str(case["expect_next_collector_term"])
                if term not in str(result.get("next_collector_command") or ""):
                    result["errors"].append(f"MAS ingest collector command 缺少锚点: {term}")
                    result["ok"] = False
            if case.get("invalid_artifact_file"):
                invalid_return_path = bind_fixture_return(
                    base_dir / str(case["invalid_artifact_file"]),
                    task_dir / "invalid-returned-artifact.json",
                    dispatch_manifest,
                )
                invalid_result = ingest_mas_artifact_file(
                    invalid_return_path,
                    task_dir,
                    through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                )
                result["invalid_result"] = invalid_result
                if invalid_result.get("ok"):
                    result["errors"].append("MAS ingest 无效 artifact 应失败但实际通过")
                    result["ok"] = False
                if invalid_result.get("ingest_status") != "invalid_artifact_not_written":
                    result["errors"].append(
                        "MAS ingest 无效 artifact 状态不符合预期: "
                        f"{invalid_result.get('ingest_status')}"
                    )
                    result["ok"] = False
                if not invalid_result.get("repair_history_file"):
                    result["errors"].append("MAS ingest 无效 artifact 未写入 repair_history_file")
                    result["ok"] = False
                if case.get("expect_reserved_field_repair"):
                    reserved_errors = "\n".join(str(item) for item in invalid_result.get("errors", []))
                    if "内部拆分字段" not in reserved_errors:
                        result["errors"].append("MAS ingest 未拒绝 subagent 伪造内部拆分字段")
                        result["ok"] = False
            if case.get("expect_duplicate_repair"):
                duplicate_result = ingest_mas_artifact_file(
                    bound_return_path,
                    task_dir,
                    through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                )
                result["duplicate_result"] = duplicate_result
                if duplicate_result.get("ok"):
                    result["errors"].append("MAS ingest 重复 artifact 应失败但实际通过")
                    result["ok"] = False
                if duplicate_result.get("ingest_status") != "duplicate_artifact_not_written":
                    result["errors"].append(
                        "MAS ingest 重复 artifact 状态不符合预期: "
                        f"{duplicate_result.get('ingest_status')}"
                    )
                    result["ok"] = False
                if not duplicate_result.get("repair_history_file"):
                    result["errors"].append("MAS ingest 重复 artifact 未写入 repair_history_file")
                    result["ok"] = False
            if case.get("expect_transaction_rollback"):
                with tempfile.TemporaryDirectory(prefix="mas-ingest-transaction-") as transaction_tmpdir:
                    transaction_task_dir = Path(transaction_tmpdir)
                    write_mas_dispatch_files(bundle, transaction_task_dir)
                    _, transaction_manifest = dispatch_context(transaction_task_dir)
                    transaction_return = bind_fixture_return(
                        artifact_input_path,
                        transaction_task_dir / "returned-artifact.json",
                        transaction_manifest,
                    )
                    transaction_artifact_dir = transaction_task_dir / "artifacts"
                    real_replace = ingest_mas_artifact_module.os.replace
                    publish_count = 0

                    def fail_second_artifact_publish(source: Any, destination: Any) -> None:
                        nonlocal publish_count
                        source_path = Path(source)
                        destination_path = Path(destination)
                        if source_path.parent.name == "stage" and destination_path.parent == transaction_artifact_dir:
                            publish_count += 1
                            if publish_count == 2:
                                raise OSError(errno.EIO, "synthetic second artifact publish failure")
                        real_replace(source, destination)

                    with patch.object(
                        ingest_mas_artifact_module.os,
                        "replace",
                        side_effect=fail_second_artifact_publish,
                    ):
                        failed_transaction = ingest_mas_artifact_file(
                            transaction_return,
                            transaction_task_dir,
                            through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                        )
                    result["transaction_failure_result"] = failed_transaction
                    if failed_transaction.get("ok") or failed_transaction.get("ingest_status") != "artifact_transaction_failed_not_written":
                        result["errors"].append("MAS ingest 多 artifact 故障未按事务失败")
                        result["ok"] = False
                    residual_artifacts = sorted(transaction_artifact_dir.glob("*.json"))
                    residual_transactions = sorted(transaction_artifact_dir.glob(".mas-ingest-txn-*"))
                    if residual_artifacts or residual_transactions:
                        result["errors"].append(
                            "MAS ingest 事务失败后残留 artifact 或事务目录: "
                            + ", ".join(str(path) for path in residual_artifacts + residual_transactions)
                        )
                        result["ok"] = False
                    retry_result = ingest_mas_artifact_file(
                        transaction_return,
                        transaction_task_dir,
                        through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                    )
                    result["transaction_retry_result"] = retry_result
                    retry_types = {
                        str(item.get("artifact_type") or "")
                        for item in retry_result.get("written_artifacts", [])
                        if isinstance(item, dict)
                    }
                    expected_retry_types = {str(item) for item in case.get("expect_written_artifacts", [])}
                    if not retry_result.get("ok") or not expected_retry_types <= retry_types:
                        result["errors"].append("MAS ingest 事务回滚后原返回无法干净重试")
                        result["ok"] = False

                    crash_task_dir = transaction_task_dir / "hard-crash-recovery"
                    write_mas_dispatch_files(bundle, crash_task_dir)
                    _, crash_manifest = dispatch_context(crash_task_dir)
                    crash_return = bind_fixture_return(
                        artifact_input_path,
                        crash_task_dir / "returned-artifact.json",
                        crash_manifest,
                    )
                    crash_script = "\n".join(
                        [
                            "import os, sys",
                            "from pathlib import Path",
                            "sys.path.insert(0, sys.argv[1])",
                            "import ingest_mas_artifact as module",
                            "artifact_dir = Path(sys.argv[3]) / 'artifacts'",
                            "real_replace = module.os.replace",
                            "state = {'publish_count': 0}",
                            "def crash_during_publish(source, destination):",
                            "    source_path = Path(source)",
                            "    destination_path = Path(destination)",
                            "    if source_path.parent.name == 'stage' and destination_path.parent == artifact_dir:",
                            "        state['publish_count'] += 1",
                            "        if state['publish_count'] == 2:",
                            "            os._exit(77)",
                            "    real_replace(source, destination)",
                            "module.os.replace = crash_during_publish",
                            "module.ingest_mas_artifact_file(Path(sys.argv[2]), Path(sys.argv[3]), through_phase=sys.argv[4] or None)",
                        ]
                    )
                    crashed_ingest = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            crash_script,
                            str(SCRIPT_DIR),
                            str(crash_return),
                            str(crash_task_dir),
                            str(case.get("through_phase") or ""),
                        ],
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        env=utf8_subprocess_env(),
                        timeout=20,
                        check=False,
                    )
                    if crashed_ingest.returncode != 77:
                        result["errors"].append(
                            "MAS ingest 硬退出注入未生效: "
                            f"returncode={crashed_ingest.returncode}"
                        )
                        result["ok"] = False
                    crash_transactions = list((crash_task_dir / "artifacts").glob(".mas-ingest-txn-*"))
                    if not crash_transactions:
                        result["errors"].append("MAS ingest 硬退出后未保留可恢复事务")
                        result["ok"] = False
                    blocked_collector = collect_mas_run(
                        crash_task_dir,
                        through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                    )
                    result["hard_crash_collector_result"] = blocked_collector
                    blocked_error_text = "\n".join(
                        str(item) for item in blocked_collector.get("errors", [])
                    )
                    if blocked_collector.get("ok") or "存在未完成 MAS artifact 事务" not in blocked_error_text:
                        result["errors"].append("MAS collector 未阻断硬退出后的半发布 artifact 集合")
                        result["ok"] = False
                    crash_recovery_result = ingest_mas_artifact_file(
                        crash_return,
                        crash_task_dir,
                        through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                    )
                    result["hard_crash_recovery_result"] = crash_recovery_result
                    crash_warning_text = "\n".join(
                        str(item) for item in crash_recovery_result.get("warnings", [])
                    )
                    if (
                        not crash_recovery_result.get("ok")
                        or "recovered 1 unfinished MAS artifact transaction" not in crash_warning_text
                        or list((crash_task_dir / "artifacts").glob(".mas-ingest-txn-*"))
                    ):
                        result["errors"].append("MAS ingest 硬退出事务未在下次 ingest 自动恢复")
                        result["ok"] = False

                    before_replacement = {
                        path.name: path.read_bytes()
                        for path in transaction_artifact_dir.glob("*.json")
                    }
                    publish_count = 0
                    with patch.object(
                        ingest_mas_artifact_module.os,
                        "replace",
                        side_effect=fail_second_artifact_publish,
                    ):
                        failed_replacement = ingest_mas_artifact_file(
                            transaction_return,
                            transaction_task_dir,
                            through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                            replace_existing=True,
                        )
                    result["transaction_replacement_failure_result"] = failed_replacement
                    after_failed_replacement = {
                        path.name: path.read_bytes()
                        for path in transaction_artifact_dir.glob("*.json")
                    }
                    if (
                        failed_replacement.get("ok")
                        or failed_replacement.get("ingest_status") != "artifact_transaction_failed_not_written"
                        or after_failed_replacement != before_replacement
                    ):
                        result["errors"].append("MAS ingest 替换事务失败后未完整恢复旧 artifact set")
                        result["ok"] = False

                    real_write_json = ingest_mas_artifact_module.write_json
                    repair_stage_count = 0

                    def fail_second_repair_stage(path: Path, staged_payload: Any) -> None:
                        nonlocal repair_stage_count
                        if path.parent.name == "stage" and path.name.startswith("repair-"):
                            repair_stage_count += 1
                            if repair_stage_count == 2:
                                raise OSError(errno.EIO, "synthetic replacement archive staging failure")
                        real_write_json(path, staged_payload)

                    superseded_before = set((transaction_task_dir / "repair_history").glob("*superseded*.json"))
                    with patch.object(
                        ingest_mas_artifact_module,
                        "write_json",
                        side_effect=fail_second_repair_stage,
                    ):
                        failed_archive_stage = ingest_mas_artifact_file(
                            transaction_return,
                            transaction_task_dir,
                            through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                            replace_existing=True,
                        )
                    result["replacement_archive_stage_failure_result"] = failed_archive_stage
                    superseded_after = set((transaction_task_dir / "repair_history").glob("*superseded*.json"))
                    after_archive_stage_failure = {
                        path.name: path.read_bytes()
                        for path in transaction_artifact_dir.glob("*.json")
                    }
                    if (
                        failed_archive_stage.get("ok")
                        or failed_archive_stage.get("ingest_status") != "artifact_transaction_failed_not_written"
                        or after_archive_stage_failure != before_replacement
                        or superseded_after != superseded_before
                    ):
                        result["errors"].append("MAS ingest 替换归档预备失败后留下半提交记录")
                        result["ok"] = False

                    publish_count = 0
                    restore_failure_count = 0

                    def fail_publish_and_first_restore(source: Any, destination: Any) -> None:
                        nonlocal publish_count, restore_failure_count
                        source_path = Path(source)
                        destination_path = Path(destination)
                        if source_path.parent.name == "stage" and destination_path.parent == transaction_artifact_dir:
                            publish_count += 1
                            if publish_count == 2:
                                raise OSError(errno.EIO, "synthetic second artifact publish failure")
                        if source_path.parent.name == "backup" and destination_path.parent == transaction_artifact_dir:
                            restore_failure_count += 1
                            if restore_failure_count == 1:
                                raise OSError(errno.EIO, "synthetic backup restore failure")
                        real_replace(source, destination)

                    with patch.object(
                        ingest_mas_artifact_module.os,
                        "replace",
                        side_effect=fail_publish_and_first_restore,
                    ):
                        recovery_required_result = ingest_mas_artifact_file(
                            transaction_return,
                            transaction_task_dir,
                            through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                            replace_existing=True,
                        )
                    result["transaction_recovery_required_result"] = recovery_required_result
                    pending_transactions = list(transaction_artifact_dir.glob(".mas-ingest-txn-*"))
                    if (
                        recovery_required_result.get("ok")
                        or recovery_required_result.get("ingest_status") != "artifact_transaction_recovery_required"
                        or not pending_transactions
                    ):
                        result["errors"].append("MAS ingest 回滚失败后未保留可重试恢复状态")
                        result["ok"] = False

                    replacement_result = ingest_mas_artifact_file(
                        transaction_return,
                        transaction_task_dir,
                        through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                        replace_existing=True,
                    )
                    result["transaction_replacement_result"] = replacement_result
                    if (
                        not replacement_result.get("ok")
                        or replacement_result.get("ingest_status") != "replaced"
                        or not replacement_result.get("repair_history_file")
                        or list(transaction_artifact_dir.glob(".mas-ingest-txn-*"))
                    ):
                        result["errors"].append("MAS ingest 显式替换未归档旧值并提交完整 artifact set")
                        result["ok"] = False
            if case.get("expect_identity_guard"):
                bound_payload = json.loads(bound_return_path.read_text(encoding="utf-8"))
                stale_payload = dict(bound_payload)
                stale_payload["run_id"] = "stale-run-id"
                stale_path = task_dir / "stale-run-return.json"
                stale_path.write_text(json.dumps(stale_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                stale_result = ingest_mas_artifact_file(stale_path, task_dir, through_phase="pre_draft")
                stale_errors = "\n".join(str(item) for item in stale_result.get("errors", []))
                if stale_result.get("ok") or "run_id 不匹配" not in stale_errors:
                    result["errors"].append("MAS ingest 未拦截跨 run artifact")
                    result["ok"] = False

                original_manifest = json.loads((task_dir / "dispatch_manifest.json").read_text(encoding="utf-8"))
                stale_manifest = dict(original_manifest)
                stale_manifest["run_id"] = "stale-manifest-run"
                (task_dir / "dispatch_manifest.json").write_text(
                    json.dumps(stale_manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                stale_manifest_result = ingest_mas_artifact_file(
                    bound_return_path,
                    task_dir,
                    through_phase="pre_draft",
                )
                stale_manifest_errors = "\n".join(str(item) for item in stale_manifest_result.get("errors", []))
                if stale_manifest_result.get("ok") or "bundle/manifest run_id 不一致" not in stale_manifest_errors:
                    result["errors"].append("MAS ingest 未拦截 stale manifest run_id")
                    result["ok"] = False
                (task_dir / "dispatch_manifest.json").write_text(
                    json.dumps(original_manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                owner_payload = {
                    "run_id": bound_payload.get("run_id"),
                    "task_id": bound_payload.get("task_id"),
                    "dispatch_phase": bound_payload.get("dispatch_phase"),
                    "artifact_owner": bound_payload.get("artifact_owner"),
                    "artifact_type": "source_manifest",
                    "artifact": {
                        "source_mode": "audio_plus_document",
                        "materials": [],
                        "archive_allowed": False,
                        "archive_status": "not_started",
                        "skipped_reason": "synthetic_identity_guard"
                    }
                }
                owner_path = task_dir / "cross-owner-return.json"
                owner_path.write_text(json.dumps(owner_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                owner_result = ingest_mas_artifact_file(owner_path, task_dir, through_phase="pre_draft")
                owner_errors = "\n".join(str(item) for item in owner_result.get("errors", []))
                if owner_result.get("ok") or "artifact_owner 必须为 Main Orchestrator" not in owner_errors:
                    result["errors"].append("MAS ingest 未拦截跨 owner artifact")
                    result["ok"] = False
                forged_main_payload = {
                    **owner_payload,
                    "task_id": f"{bound_payload.get('run_id')}:main:source_manifest",
                    "dispatch_phase": "pre_draft",
                    "artifact_owner": "Main Orchestrator",
                    "artifact": {
                        **owner_payload["artifact"],
                        "materials": [
                            {"kind": "audio", "name": "synthetic_meeting.wav"},
                            {"kind": "document", "name": "provided_transcript.md"},
                        ],
                    },
                }
                forged_main_path = task_dir / "forged-main-owned-return.json"
                forged_main_path.write_text(
                    json.dumps(forged_main_payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                forged_main_result = ingest_mas_artifact_file(
                    forged_main_path,
                    task_dir,
                    through_phase="pre_draft",
                )
                forged_main_errors = "\n".join(str(item) for item in forged_main_result.get("errors", []))
                if forged_main_result.get("ok") or "不接受 Main Orchestrator 自有 artifact" not in forged_main_errors:
                    result["errors"].append("MAS ingest 未拒绝伪造的 main-owned artifact")
                    result["ok"] = False
    elif case.get("check") == "mas_plan_summary":
        summary = json.loads(file_path.read_text(encoding="utf-8"))
        result = plan_from_summary(summary)
        expected_status = str(case.get("expect_plan_status") or "")
        if expected_status and result.get("plan_status") != expected_status:
            result["errors"].append(
                f"MAS plan summary status 不符合预期: expected={expected_status} actual={result.get('plan_status')}"
            )
            result["ok"] = False
        plan_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in plan_text:
                result["errors"].append(f"MAS plan summary 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_next_action_plan":
        request_payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(request_payload, dict):
            raise ValueError(f"MAS task request 必须是 JSON object: {file_path}")
        artifact_fixture_path = base_dir / str(case["artifact_file"])
        artifact_payload = json.loads(artifact_fixture_path.read_text(encoding="utf-8"))
        fixture_artifacts = artifact_payload.get("artifacts")
        if not isinstance(fixture_artifacts, dict):
            raise ValueError(f"MAS artifact fixture 必须包含 artifacts object: {artifact_fixture_path}")
        bundle = build_mas_task_bundle_from_request(request_payload)
        errors = validate_mas_task_bundle(bundle)
        warnings: list[str] = []
        omitted = {str(item) for item in case.get("omit_artifacts", [])}
        with tempfile.TemporaryDirectory(prefix="mas-next-action-plan-") as tmpdir:
            task_dir = Path(tmpdir)
            write_mas_dispatch_files(bundle, task_dir)
            _, dispatch_manifest = dispatch_context(task_dir)
            synthetic_markdown = task_dir / "synthetic-final.md"
            synthetic_markdown.write_text(synthetic_final_markdown(fixture_artifacts), encoding="utf-8")
            artifact_dir = task_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            deferred_export: tuple[str, Any] | None = None
            for artifact_type, artifact in fixture_artifacts.items():
                artifact_type = str(artifact_type)
                if artifact_type in omitted or artifact_type not in bundle.get("expected_artifacts", []):
                    continue
                if case.get("record_main_actions") and artifact_type == "export_manifest":
                    deferred_export = (artifact_type, artifact)
                    continue
                payload = fixture_payload(dispatch_manifest, artifact_type, artifact, synthetic_markdown)
                (artifact_dir / f"{artifact_type}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            if case.get("record_main_actions"):
                summary = collect_mas_run(task_dir, through_phase="draft_review")
                summary_path = task_dir / "mas_run_summary.json"
                summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                record_main_actions(task_dir, synthetic_markdown, summary_path=summary_path)
                if deferred_export:
                    artifact_type, artifact = deferred_export
                    payload = fixture_payload(dispatch_manifest, artifact_type, artifact, synthetic_markdown)
                    (artifact_dir / f"{artifact_type}.json").write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            summary = collect_mas_run(
                task_dir,
                through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
            )
            result = plan_from_summary(summary)
            result["errors"] = errors + result["errors"]
            result["warnings"] = warnings + result["warnings"]
            result["ok"] = not result["errors"] and bool(result["ok"])
        expected_status = case.get("expect_plan_status")
        if expected_status and result.get("plan_status") != expected_status:
            result["errors"].append(
                f"MAS next-action plan status 不符合预期: expected={expected_status} "
                f"actual={result.get('plan_status')}"
            )
            result["ok"] = False
        expected_action_type = case.get("expect_next_action_type")
        if expected_action_type and result.get("next_action_type") != expected_action_type:
            result["errors"].append(
                f"MAS next-action plan next_action_type 不符合预期: expected={expected_action_type} "
                f"actual={result.get('next_action_type')}"
            )
            result["ok"] = False
        expected_phase = case.get("expect_phase")
        if expected_phase and result.get("phase") != expected_phase:
            result["errors"].append(
                f"MAS next-action plan phase 不符合预期: expected={expected_phase} "
                f"actual={result.get('phase')}"
            )
            result["ok"] = False
        dispatch_artifacts = {
            str(item.get("artifact_type") or "")
            for item in result.get("dispatch_tasks", [])
            if isinstance(item, dict)
        }
        for artifact in [str(item) for item in case.get("expect_dispatch_artifacts", [])]:
            if artifact not in dispatch_artifacts:
                result["errors"].append(f"MAS next-action plan 缺少 dispatch artifact: {artifact}")
                result["ok"] = False
        plan_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in plan_text:
                result["errors"].append(f"MAS next-action plan 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_phase_operator":
        artifact_fixture_path = base_dir / str(case["artifact_file"])
        artifact_payload = json.loads(artifact_fixture_path.read_text(encoding="utf-8"))
        fixture_artifacts = artifact_payload.get("artifacts")
        if not isinstance(fixture_artifacts, dict):
            raise ValueError(f"MAS artifact fixture 必须包含 artifacts object: {artifact_fixture_path}")
        with tempfile.TemporaryDirectory(prefix="mas-phase-operator-") as tmpdir:
            tmp_path = Path(tmpdir)
            task_dir = tmp_path / "dispatch"
            request_payload = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(request_payload, dict):
                raise ValueError(f"MAS phase operator request must be a JSON object: {file_path}")
            initialize_with_request = bool(case.get("initialize_with_request"))
            if initialize_with_request and case.get("return_artifacts"):
                raise ValueError("initialize_with_request regression cannot pre-bind return artifacts")
            if initialize_with_request:
                dispatch_manifest: dict[str, Any] = {}
            else:
                bundle = build_mas_task_bundle_from_request(request_payload)
                write_mas_dispatch_files(bundle, task_dir)
                _, dispatch_manifest = dispatch_context(task_dir)
            returns_dir = tmp_path / "returns"
            returns_dir.mkdir(parents=True, exist_ok=True)
            return_paths: list[Path] = []
            emitted_task_ids: set[str] = set()
            errors: list[str] = []
            for artifact_type in [str(item) for item in case.get("return_artifacts", [])]:
                if artifact_type not in fixture_artifacts:
                    errors.append(f"MAS phase operator fixture 缺少 return artifact: {artifact_type}")
                    continue
                identity = fixture_identity(dispatch_manifest, artifact_type)
                task_id = str(identity.get("task_id") or "")
                if task_id in emitted_task_ids:
                    continue
                emitted_task_ids.add(task_id)
                return_path = returns_dir / f"{artifact_type}.json"
                return_path.write_text(
                    json.dumps(
                        fixture_return_payload(dispatch_manifest, artifact_type, fixture_artifacts),
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                return_paths.append(return_path)
            result = run_mas_phase_operator(
                task_dir=task_dir,
                request_path=file_path if initialize_with_request else None,
                return_paths=return_paths,
                through_phase=str(case["through_phase"]) if case.get("through_phase") else None,
                auto_source_manifest=bool(case.get("auto_source_manifest", False)),
            )
            result["errors"] = errors + result["errors"]
            result["ok"] = not result["errors"] and bool(result["ok"])
            for key in [
                "collector_summary_file",
                "combined_artifacts_file",
                "next_action_plan_file",
                "operator_state_file",
            ]:
                output_path = Path(str(result.get(key) or ""))
                if not output_path.exists():
                    result["errors"].append(f"MAS phase operator 未写入输出文件: {key}")
                    result["ok"] = False
        expected_operator_status = case.get("expect_operator_status")
        if expected_operator_status and result.get("operator_status") != expected_operator_status:
            result["errors"].append(
                f"MAS phase operator status 不符合预期: expected={expected_operator_status} "
                f"actual={result.get('operator_status')}"
            )
            result["ok"] = False
        expected_plan_status = case.get("expect_plan_status")
        if expected_plan_status and result.get("plan_status") != expected_plan_status:
            result["errors"].append(
                f"MAS phase operator plan_status 不符合预期: expected={expected_plan_status} "
                f"actual={result.get('plan_status')}"
            )
            result["ok"] = False
        expected_action_type = case.get("expect_next_action_type")
        if expected_action_type and result.get("next_action_type") != expected_action_type:
            result["errors"].append(
                f"MAS phase operator next_action_type 不符合预期: expected={expected_action_type} "
                f"actual={result.get('next_action_type')}"
            )
            result["ok"] = False
        expected_phase = case.get("expect_phase")
        if expected_phase and result.get("phase") != expected_phase:
            result["errors"].append(
                f"MAS phase operator phase 不符合预期: expected={expected_phase} "
                f"actual={result.get('phase')}"
            )
            result["ok"] = False
        dispatch_artifacts = {
            str(item.get("artifact_type") or "")
            for item in result.get("dispatch_tasks", [])
            if isinstance(item, dict)
        }
        for artifact in [str(item) for item in case.get("expect_dispatch_artifacts", [])]:
            if artifact not in dispatch_artifacts:
                result["errors"].append(f"MAS phase operator 缺少 dispatch artifact: {artifact}")
                result["ok"] = False
        main_owned_artifacts = {str(item) for item in result.get("main_owned_missing_artifacts", [])}
        for artifact in [str(item) for item in case.get("expect_main_owned_artifacts", [])]:
            if artifact not in main_owned_artifacts:
                result["errors"].append(f"MAS phase operator 缺少 main-owned artifact: {artifact}")
                result["ok"] = False
        for artifact in [str(item) for item in case.get("forbid_main_owned_artifacts", [])]:
            if artifact in main_owned_artifacts:
                result["errors"].append(f"MAS phase operator 不应缺少 main-owned artifact: {artifact}")
                result["ok"] = False
        expected_auto_status = case.get("expect_auto_source_manifest_status")
        if expected_auto_status:
            auto_result = result.get("auto_source_manifest", {})
            if not isinstance(auto_result, dict) or auto_result.get("status") != expected_auto_status:
                result["errors"].append(
                    "MAS phase operator auto_source_manifest status 不符合预期: "
                    f"expected={expected_auto_status} actual={auto_result.get('status') if isinstance(auto_result, dict) else auto_result}"
                )
                result["ok"] = False
        operator_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in operator_text:
                result["errors"].append(f"MAS phase operator 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_phase_operator_full_loop":
        artifact_fixture_path = base_dir / str(case["artifact_file"])
        artifact_payload = json.loads(artifact_fixture_path.read_text(encoding="utf-8"))
        fixture_artifacts = artifact_payload.get("artifacts")
        if not isinstance(fixture_artifacts, dict):
            raise ValueError(f"MAS artifact fixture 必须包含 artifacts object: {artifact_fixture_path}")
        runs: list[dict[str, Any]] = []
        errors: list[str] = []
        warnings: list[str] = []
        with tempfile.TemporaryDirectory(prefix="mas-phase-operator-full-loop-") as tmpdir:
            tmp_path = Path(tmpdir)
            task_dir = tmp_path / "dispatch"
            request_payload = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(request_payload, dict):
                raise ValueError(f"MAS phase operator request must be a JSON object: {file_path}")
            bundle = build_mas_task_bundle_from_request(request_payload)
            write_mas_dispatch_files(bundle, task_dir)
            _, dispatch_manifest = dispatch_context(task_dir)
            synthetic_markdown = task_dir / "synthetic-final.md"
            synthetic_markdown.write_text(synthetic_final_markdown(fixture_artifacts), encoding="utf-8")
            (task_dir / "synthetic.verification.json").write_text(
                json.dumps(synthetic_verification_payload(fixture_artifacts), ensure_ascii=False, indent=2)
                + "\n",
                encoding="utf-8",
            )
            for index, run_spec in enumerate(case.get("runs", []), start=1):
                if not isinstance(run_spec, dict):
                    errors.append("MAS phase operator full-loop run spec 必须是 JSON object")
                    continue
                returns_dir = tmp_path / f"returns-{index:02d}"
                returns_dir.mkdir(parents=True, exist_ok=True)
                return_paths: list[Path] = []
                emitted_task_ids: set[str] = set()
                if run_spec.get("record_main_actions"):
                    record_main_actions(
                        task_dir,
                        synthetic_markdown,
                        summary_path=task_dir / "mas_run_summary.json",
                        replace=(task_dir / "artifacts" / "main_action_receipt.json").exists(),
                    )
                for artifact_type in [str(item) for item in run_spec.get("return_artifacts", [])]:
                    if artifact_type not in fixture_artifacts:
                        errors.append(f"MAS phase operator full-loop fixture 缺少 artifact: {artifact_type}")
                        continue
                    identity = fixture_identity(dispatch_manifest, artifact_type)
                    task_id = str(identity.get("task_id") or "")
                    if task_id in emitted_task_ids:
                        continue
                    emitted_task_ids.add(task_id)
                    return_path = returns_dir / f"{artifact_type}.json"
                    return_path.write_text(
                        json.dumps(
                            fixture_return_payload(
                                dispatch_manifest,
                                artifact_type,
                                fixture_artifacts,
                                synthetic_markdown,
                            ),
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    return_paths.append(return_path)
                run_result = run_mas_phase_operator(
                    task_dir=task_dir,
                    request_path=None,
                    return_paths=return_paths,
                    through_phase=str(run_spec["through_phase"]) if run_spec.get("through_phase") else None,
                    auto_source_manifest=bool(run_spec.get("auto_source_manifest", False)),
                )
                runs.append(
                    {
                        "index": index,
                        "ok": bool(run_result.get("ok")),
                        "operator_status": run_result.get("operator_status"),
                        "phase": run_result.get("phase"),
                        "next_action_type": run_result.get("next_action_type"),
                        "collector_ok": bool(run_result.get("collector_ok")),
                        "main_actions": run_result.get("main_actions", []),
                        "main_action_checklist": run_result.get("main_action_checklist", []),
                        "dispatch_artifacts": [
                            str(item.get("artifact_type") or "")
                            for item in run_result.get("dispatch_tasks", [])
                            if isinstance(item, dict)
                        ],
                        "errors": run_result.get("errors", []),
                        "warnings": run_result.get("warnings", []),
                    }
                )
        result = {
            "ok": not errors and all(bool(item.get("ok")) for item in runs),
            "errors": errors,
            "warnings": warnings,
            "runs": runs,
        }
        expected_runs = case.get("expect_runs", [])
        for expected in expected_runs:
            if not isinstance(expected, dict):
                result["errors"].append("MAS phase operator full-loop expect_runs item 必须是 JSON object")
                result["ok"] = False
                continue
            index = int(expected.get("index") or 0)
            actual = next((item for item in runs if int(item.get("index") or 0) == index), None)
            if not actual:
                result["errors"].append(f"MAS phase operator full-loop 缺少 run: {index}")
                result["ok"] = False
                continue
            for field_name in ["operator_status", "phase", "next_action_type"]:
                if expected.get(field_name) and actual.get(field_name) != expected.get(field_name):
                    result["errors"].append(
                        f"MAS phase operator full-loop run {index} {field_name} 不符合预期: "
                        f"expected={expected.get(field_name)} actual={actual.get(field_name)}"
                    )
                    result["ok"] = False
            for artifact in [str(item) for item in expected.get("dispatch_artifacts", [])]:
                if artifact not in actual.get("dispatch_artifacts", []):
                    result["errors"].append(
                        f"MAS phase operator full-loop run {index} 缺少 dispatch artifact: {artifact}"
                    )
                    result["ok"] = False
        final_run = runs[-1] if runs else {}
        for action in [str(item) for item in case.get("expect_final_main_actions", [])]:
            if action not in final_run.get("main_actions", []):
                result["errors"].append(f"MAS phase operator full-loop final 缺少 main_action: {action}")
                result["ok"] = False
        trace_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in trace_text:
                result["errors"].append(f"MAS phase operator full-loop 缺少文本锚点: {term}")
                result["ok"] = False
    elif case.get("check") == "mas_live_pilot_trace":
        trace = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(trace, dict):
            raise ValueError(f"MAS live pilot trace 必须是 JSON object: {file_path}")
        errors = []
        warnings = []
        if trace.get("schema_version") != "1.0":
            errors.append(f"MAS live pilot trace schema_version 不符合预期: {trace.get('schema_version')}")
        if trace.get("execution_mode") != "codex_subagent_synthetic_pilot":
            errors.append(
                "MAS live pilot trace execution_mode 不符合预期: "
                f"{trace.get('execution_mode')}"
            )
        if int(trace.get("subagent_task_count", 0)) < 5:
            errors.append(f"MAS live pilot trace subagent_task_count 过低: {trace.get('subagent_task_count')}")
        if not bool(trace.get("repair_loop_observed")):
            errors.append("MAS live pilot trace 缺少 repair_loop_observed=true")
        boundaries = trace.get("boundaries")
        for boundary in [str(item) for item in case.get("require_boundaries", [])]:
            if not isinstance(boundaries, dict) or boundaries.get(boundary) is not True:
                errors.append(f"MAS live pilot trace 缺少边界确认: {boundary}")
        phases = trace.get("phases")
        if not isinstance(phases, list):
            errors.append("MAS live pilot trace phases 必须是 JSON array")
            phases = []
        phase_results = {
            str(item.get("phase") or ""): item
            for item in phases
            if isinstance(item, dict)
        }
        for expected_phase_action in case.get("expect_phase_next_actions", []):
            if not isinstance(expected_phase_action, dict):
                errors.append("MAS live pilot trace expect_phase_next_actions item 必须是 JSON object")
                continue
            phase = str(expected_phase_action.get("phase") or "")
            phase_result = phase_results.get(phase)
            if not phase_result:
                errors.append(f"MAS live pilot trace 缺少 phase 结果: {phase}")
                continue
            if "collector_ok" in expected_phase_action and bool(phase_result.get("collector_ok")) != bool(
                expected_phase_action.get("collector_ok")
            ):
                errors.append(
                    f"MAS live pilot trace {phase} collector_ok 不符合预期: "
                    f"expected={bool(expected_phase_action.get('collector_ok'))} "
                    f"actual={bool(phase_result.get('collector_ok'))}"
                )
            expected_type = expected_phase_action.get("type")
            if expected_type and phase_result.get("next_action_type") != expected_type:
                errors.append(
                    f"MAS live pilot trace {phase} next_action 不符合预期: "
                    f"expected={expected_type} actual={phase_result.get('next_action_type')}"
                )
            if "next_phase" in expected_phase_action:
                expected_phase = str(expected_phase_action.get("next_phase") or "")
                if str(phase_result.get("next_phase") or "") != expected_phase:
                    errors.append(
                        f"MAS live pilot trace {phase} next_phase 不符合预期: "
                        f"expected={expected_phase} actual={phase_result.get('next_phase')}"
                    )
        expected_decision = case.get("expect_final_decision")
        if expected_decision and trace.get("final_decision") != expected_decision:
            errors.append(
                f"MAS live pilot trace final_decision 不符合预期: "
                f"expected={expected_decision} actual={trace.get('final_decision')}"
            )
        final_actions = [str(item) for item in trace.get("final_main_actions", [])]
        for action in [str(item) for item in case.get("require_final_actions", [])]:
            if action not in final_actions:
                errors.append(f"MAS live pilot trace 缺少 final_main_action: {action}")
        trace_text = json.dumps(trace, ensure_ascii=False, sort_keys=True)
        for term in [str(item) for item in case.get("required_terms", [])]:
            if term not in trace_text:
                errors.append(f"MAS live pilot trace 缺少文本锚点: {term}")
        result = {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "phase_count": len(phases),
        }
    elif case.get("check") == "mas_decision":
        result = summarize_mas_decision_file(
            file_path,
            required_artifacts=[str(item) for item in case.get("require_artifacts", [])],
        )
        expected_decision = case.get("expect_decision")
        if expected_decision and result.get("decision") != expected_decision:
            result["errors"].append(
                f"MAS decision 不符合预期: expected={expected_decision} actual={result.get('decision')}"
            )
            result["ok"] = False
        actions = [str(item) for item in result.get("main_actions", [])]
        for action in [str(item) for item in case.get("require_actions", [])]:
            if action not in actions:
                result["errors"].append(f"MAS decision 缺少 main_action: {action}")
                result["ok"] = False
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
            if verification_result["ok"]:
                result["errors"].extend(inline_doubtful_mapping_findings(markdown, verification_path))
            result["ok"] = result["ok"] and verification_result["ok"]
            result["ok"] = result["ok"] and not result["errors"]
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
    result.setdefault("errors", [])
    result.setdefault("warnings", [])
    raw_ok = bool(result["ok"])
    expect_fail = bool(case.get("expect_fail"))
    required_error_terms = [str(term) for term in case.get("required_error_terms", [])]
    required_warning_terms = [str(term) for term in case.get("required_warning_terms", [])]
    forbidden_warning_terms = [str(term) for term in case.get("forbidden_warning_terms", [])]
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
    if not expect_fail and (required_warning_terms or forbidden_warning_terms):
        for term in required_warning_terms:
            if term not in warning_text:
                expectation_errors.append(f"样例缺少预期 warning 片段: {term}")
        for term in forbidden_warning_terms:
            if term in warning_text:
                expectation_errors.append(f"样例包含禁止 warning 片段: {term}")
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
    try:
        with windows_subprocess_fcntl_shim():
            results = [run_case(case, base_dir) for case in read_cases(cases_path)]
    except Exception as exc:
        payload = {
            "ok": False,
            "case_count": 0,
            "errors": [f"回归运行失败: {exc.__class__.__name__}: {exc}"],
            "results": [],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(payload["errors"][0], file=sys.stderr)
        return 1
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
