#!/usr/bin/env python3
"""High-quality local transcription wrapper for SenseVoice."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
import wave
from pathlib import Path

DEFAULT_SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
DEFAULT_VAD_MODEL = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
DEFAULT_LOCAL_CACHE = Path.home() / "Documents/Codex/asr-model-cache"
DEFAULT_AUDIO_CHUNK_SECONDS = 60
MAX_RELIABLE_VAD_SEGMENT_MS = 10000
DEFAULT_SENSEVOICE_SEGMENT_BATCH_SIZE = 6
MODEL_ALIASES = {
    "iic/SenseVoiceSmall": ("modelscope", "models/iic/SenseVoiceSmall"),
    "SenseVoiceSmall": ("modelscope", "models/iic/SenseVoiceSmall"),
    DEFAULT_VAD_MODEL: (
        "modelscope",
        "models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    ),
    "fsmn-vad": (
        "modelscope",
        "models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
    ),
}
REQUIRED_MODEL_FILES = {
    "iic/SenseVoiceSmall": ("config.yaml", "model.pt"),
    "SenseVoiceSmall": ("config.yaml", "model.pt"),
    DEFAULT_VAD_MODEL: ("config.yaml", "model.pt"),
    "fsmn-vad": ("config.yaml", "model.pt"),
}
SENSEVOICE_TEXT_FORMATS = {"txt", "json", "all"}
SENSEVOICE_MANAGED_OUTPUT_SUFFIXES = (
    ".txt",
    ".json",
    ".timestamp_index.json",
    ".paraformer.txt",
    ".paraformer.timestamp_index.json",
)


class SenseVoiceSegmentPreparationError(RuntimeError):
    """The VAD path failed before any effective SenseVoice segment inference."""


@contextmanager
def _sensevoice_stem_lock(output_dir: Path, stem: str):
    """Serialize model work and output commits for the same output stem."""
    lock_path = output_dir / f".{stem}.sensevoice.lock"
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _path_present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _write_synced_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())


def _discard_sensevoice_transaction(transaction_dir: Path) -> None:
    if not transaction_dir.exists():
        return
    cleanup_name = transaction_dir.name.replace(".sensevoice-txn-", ".sensevoice-cleanup-", 1)
    cleanup_dir = transaction_dir.with_name(cleanup_name)
    os.replace(transaction_dir, cleanup_dir)
    shutil.rmtree(cleanup_dir, ignore_errors=True)


def _rollback_sensevoice_transaction(
    transaction_dir: Path,
    output_dir: Path,
    stem: str,
    desired_suffixes: set[str],
) -> None:
    stage_dir = transaction_dir / "stage"
    backup_dir = transaction_dir / "backup"
    restore_dir = transaction_dir / "restore"
    restore_dir.mkdir(exist_ok=True)
    for suffix in SENSEVOICE_MANAGED_OUTPUT_SUFFIXES:
        final_path = output_dir / f"{stem}{suffix}"
        stage_path = stage_dir / suffix.removeprefix(".")
        backup_path = backup_dir / suffix.removeprefix(".")
        if _path_present(backup_path):
            restore_path = restore_dir / suffix.removeprefix(".")
            restore_path.unlink(missing_ok=True)
            shutil.copy2(backup_path, restore_path, follow_symlinks=False)
            os.replace(restore_path, final_path)
        elif suffix in desired_suffixes and not _path_present(stage_path):
            final_path.unlink(missing_ok=True)
    _discard_sensevoice_transaction(transaction_dir)


def _recover_sensevoice_transactions(output_dir: Path, stem: str) -> None:
    escaped_stem = glob.escape(stem)
    for cleanup_dir in output_dir.glob(f".{escaped_stem}.sensevoice-cleanup-*"):
        if cleanup_dir.is_dir():
            shutil.rmtree(cleanup_dir, ignore_errors=True)
    transactions = sorted(
        (path for path in output_dir.glob(f".{escaped_stem}.sensevoice-txn-*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    for transaction_dir in transactions:
        if (transaction_dir / "committed").exists():
            _discard_sensevoice_transaction(transaction_dir)
            continue
        manifest_path = transaction_dir / "manifest.json"
        backup_dir = transaction_dir / "backup"
        if not manifest_path.is_file():
            if backup_dir.is_dir() and any(backup_dir.iterdir()):
                raise RuntimeError(f"SenseVoice 遗留事务缺少 manifest，无法安全恢复: {transaction_dir}")
            _discard_sensevoice_transaction(transaction_dir)
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"SenseVoice 遗留事务 manifest 损坏，无法安全恢复: {transaction_dir}: {exc}") from exc
        desired = manifest.get("desired_suffixes") if isinstance(manifest, dict) else None
        if not isinstance(desired, list) or any(item not in SENSEVOICE_MANAGED_OUTPUT_SUFFIXES for item in desired):
            raise RuntimeError(f"SenseVoice 遗留事务 manifest 含非法输出后缀: {transaction_dir}")
        if not (transaction_dir / "backup_started").exists():
            _discard_sensevoice_transaction(transaction_dir)
            continue
        _rollback_sensevoice_transaction(transaction_dir, output_dir, stem, set(desired))


def _commit_sensevoice_outputs(output_dir: Path, stem: str, rendered: dict[str, str]) -> None:
    """Replace one stem's managed outputs as a rollback-capable snapshot."""
    unknown_suffixes = set(rendered) - set(SENSEVOICE_MANAGED_OUTPUT_SUFFIXES)
    if unknown_suffixes:
        raise ValueError(f"未知 SenseVoice 受管输出后缀: {', '.join(sorted(unknown_suffixes))}")

    transaction_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{stem}.sensevoice-txn-",
            dir=output_dir,
        )
    )
    try:
        stage_dir = transaction_dir / "stage"
        backup_dir = transaction_dir / "backup"
        stage_dir.mkdir()
        backup_dir.mkdir()
        for suffix, content in rendered.items():
            (stage_dir / suffix.removeprefix(".")).write_text(content, encoding="utf-8")
        _write_synced_text(
            transaction_dir / "manifest.json",
            json.dumps({"desired_suffixes": list(rendered)}, ensure_ascii=False, sort_keys=True) + "\n",
        )
        _write_synced_text(transaction_dir / "backup_started", "started\n")
        for suffix in SENSEVOICE_MANAGED_OUTPUT_SUFFIXES:
            final_path = output_dir / f"{stem}{suffix}"
            if _path_present(final_path):
                os.replace(final_path, backup_dir / suffix.removeprefix("."))
        for suffix in rendered:
            os.replace(
                stage_dir / suffix.removeprefix("."),
                output_dir / f"{stem}{suffix}",
            )
        _write_synced_text(transaction_dir / "committed", "committed\n")
    except BaseException:
        if transaction_dir.exists():
            if (transaction_dir / "backup_started").exists():
                _rollback_sensevoice_transaction(transaction_dir, output_dir, stem, set(rendered))
            else:
                _discard_sensevoice_transaction(transaction_dir)
        raise
    else:
        try:
            _discard_sensevoice_transaction(transaction_dir)
        except OSError as exc:
            warnings.warn(
                f"SenseVoice 输出已完整提交，但事务目录清理失败；下次同 stem 运行会重试: {transaction_dir}: {exc}",
                RuntimeWarning,
            )


def _configure_model_cache(cache_dir: str) -> None:
    cache_value = (
        cache_dir
        or os.environ.get("SENSEVOICE_MODEL_CACHE")
        or os.environ.get("FUNASR_MODEL_CACHE")
        or str(DEFAULT_LOCAL_CACHE)
    ).strip()
    root = Path(cache_value).expanduser().resolve()
    if cache_dir.strip():
        os.environ["SENSEVOICE_MODEL_CACHE"] = str(root)
        os.environ["FUNASR_MODEL_CACHE"] = str(root)
        os.environ["MODELSCOPE_CACHE"] = str(root / "modelscope")
        os.environ["HF_HOME"] = str(root / "huggingface")
        return
    os.environ.setdefault("SENSEVOICE_MODEL_CACHE", str(root))
    os.environ.setdefault("FUNASR_MODEL_CACHE", str(root))
    os.environ.setdefault("MODELSCOPE_CACHE", str(root / "modelscope"))
    os.environ.setdefault("HF_HOME", str(root / "huggingface"))


def _model_cache_root() -> Path | None:
    candidates = [
        os.environ.get("SENSEVOICE_MODEL_CACHE"),
        os.environ.get("FUNASR_MODEL_CACHE"),
        os.environ.get("MODELSCOPE_CACHE"),
        DEFAULT_LOCAL_CACHE,
        Path.home() / ".cache/modelscope/hub",
        Path.home() / ".cache/modelscope",
    ]
    for value in candidates:
        if not value:
            continue
        path = Path(value).expanduser().resolve()
        if path.exists():
            return path
    return None


def _is_complete_model_dir(path: Path, model_name: str) -> bool:
    required_files = REQUIRED_MODEL_FILES.get(model_name, ("config.yaml",))
    return path.is_dir() and all((path / item).exists() for item in required_files)


def _model_candidates(model_name: str) -> list[Path]:
    alias = MODEL_ALIASES.get(model_name)
    root = _model_cache_root()
    if not alias or not root:
        return []
    source, relative_path = alias
    candidates = [
        root / source / relative_path,
        root / relative_path,
        root / "hub" / relative_path,
    ]
    if source == "modelscope" and relative_path.startswith("models/"):
        candidates.append(root / source / relative_path.removeprefix("models/"))
        candidates.append(root / relative_path.removeprefix("models/"))
        candidates.append(root / "hub" / relative_path.removeprefix("models/"))
        candidates.append(root / "hub" / "models" / relative_path.removeprefix("models/"))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _resolve_model_ref(model_name: str, *, allow_remote_model_lookup: bool = False) -> str:
    path = Path(model_name).expanduser()
    if path.exists() and _is_complete_model_dir(path, model_name):
        return str(path.resolve())
    for candidate in _model_candidates(model_name):
        if _is_complete_model_dir(candidate, model_name):
            return str(candidate)
    if not allow_remote_model_lookup:
        status = _model_cache_status(model_name)
        checked = ", ".join(str(item) for item in status.get("checked_paths", []) or [])
        missing = ", ".join(str(item) for item in status.get("missing", []) or [])
        detail = f"；检查路径: {checked}" if checked else ""
        missing_detail = f"；缺少: {missing}" if missing else ""
        raise FileNotFoundError(
            f"本地模型缓存不完整，已阻止远程模型查找: {model_name}{missing_detail}{detail}"
        )
    return model_name


def _model_cache_status(model_name: str) -> dict[str, object]:
    candidates = _model_candidates(model_name)
    if not candidates:
        return {"model": model_name, "path": "", "exists": False, "complete": False, "missing": ["cache root"]}
    required_files = REQUIRED_MODEL_FILES.get(model_name, ("config.yaml",))
    candidate = next((item for item in candidates if _is_complete_model_dir(item, model_name)), candidates[0])
    missing = [item for item in required_files if not (candidate / item).exists()]
    return {
        "model": model_name,
        "path": str(candidate),
        "exists": candidate.exists(),
        "complete": candidate.exists() and not missing,
        "missing": missing,
        "checked_paths": [str(item) for item in candidates],
    }


def _model_cache_report() -> dict[str, object]:
    return {
        "cache_root": str(_model_cache_root() or ""),
        "models": {
            "sensevoice": _model_cache_status(DEFAULT_SENSEVOICE_MODEL),
            "vad": _model_cache_status(DEFAULT_VAD_MODEL),
        },
    }


def _print_model_cache_report() -> None:
    print(json.dumps(_model_cache_report(), ensure_ascii=False, indent=2))


def _ensure_ffmpeg_in_path(env: dict[str, str]) -> dict[str, str]:
    if shutil.which("ffmpeg"):
        return env

    try:
        import imageio_ffmpeg  # type: ignore

        ffmpeg_exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
        tmp_dir = Path(tempfile.mkdtemp(prefix="ffmpeg-bin-"))
        link = tmp_dir / "ffmpeg"
        if not link.exists():
            link.symlink_to(ffmpeg_exe)
        env["PATH"] = f"{tmp_dir}:{env.get('PATH', '')}"
    except Exception:
        pass

    return env


def _ensure_ffmpeg_for_current_process() -> None:
    env = _ensure_ffmpeg_in_path(dict(os.environ))
    if env.get("PATH") != os.environ.get("PATH"):
        os.environ["PATH"] = env["PATH"]


def _audio_file_chunks(input_file: Path, *, chunk_seconds: int = DEFAULT_AUDIO_CHUNK_SECONDS) -> tuple[tempfile.TemporaryDirectory[str], list[Path]]:
    """Create file-level chunks so long recordings are never sent to FunASR at once."""
    temp_dir = tempfile.TemporaryDirectory(prefix="sensevoice-chunks-")
    chunk_dir = Path(temp_dir.name)
    output_pattern = chunk_dir / "chunk_%05d.wav"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        temp_dir.cleanup()
        raise RuntimeError("ffmpeg unavailable for audio chunking")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_file),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "segment",
        "-segment_time",
        str(chunk_seconds),
        "-reset_timestamps",
        "1",
        str(output_pattern),
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    chunks = sorted(chunk_dir.glob("chunk_*.wav"))
    if completed.returncode != 0 or not chunks:
        error = (completed.stderr or completed.stdout or "ffmpeg did not produce audio chunks").strip()
        temp_dir.cleanup()
        raise RuntimeError(error)
    return temp_dir, chunks


def _normalize_audio_to_wav(input_file: Path, output_file: Path) -> None:
    """Normalize the full input once for the VAD and segment-slicing path."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg unavailable for VAD audio normalization")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_file),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(output_file),
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0 or not output_file.exists():
        error = (completed.stderr or completed.stdout or "ffmpeg did not produce normalized WAV").strip()
        raise RuntimeError(error)


def _parse_milliseconds(value: object) -> int | None:
    try:
        ms = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if ms < 0:
        return None
    return ms


def _extract_vad_segments(result: object) -> list[tuple[int, int]]:
    """Extract global fsmn-vad millisecond ranges from FunASR output."""
    chunks = result if isinstance(result, list) else [result]
    segments: list[tuple[int, int]] = []
    for chunk in chunks:
        values: object
        if isinstance(chunk, dict):
            values = chunk.get("value") or chunk.get("segments") or chunk.get("timestamp") or []
        else:
            values = chunk
        if not isinstance(values, list):
            continue
        for item in values:
            pair = _timestamp_pair_ms(item)
            if pair is None:
                continue
            start_ms = _parse_milliseconds(pair[0])
            end_ms = _parse_milliseconds(pair[1])
            if start_ms is None or end_ms is None or end_ms <= start_ms:
                continue
            segments.append((start_ms, end_ms))
    return segments


def _split_vad_segments_for_reliable_timestamps(
    segments: list[tuple[int, int]],
    *,
    max_duration_ms: int = MAX_RELIABLE_VAD_SEGMENT_MS,
) -> list[tuple[int, int]]:
    reliable_segments: list[tuple[int, int]] = []
    for start_ms, end_ms in segments:
        cursor = start_ms
        while cursor < end_ms:
            split_end_ms = min(cursor + max_duration_ms, end_ms)
            if split_end_ms > cursor:
                reliable_segments.append((cursor, split_end_ms))
            cursor = split_end_ms
    return reliable_segments


def _audio_segment_to_wav(input_file: Path, output_file: Path, start_ms: int, end_ms: int) -> None:
    """Copy a time range from the already-normalized PCM WAV without ffmpeg."""
    if end_ms <= start_ms:
        raise ValueError(f"invalid VAD segment range: {start_ms}-{end_ms} ms")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with wave.open(str(input_file), "rb") as source:
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() != 16000
                or source.getcomptype() != "NONE"
            ):
                raise RuntimeError("VAD segment source must be normalized PCM s16le/mono/16k WAV")
            frame_rate = source.getframerate()
            start_frame = int(round(start_ms * frame_rate / 1000))
            end_frame = int(round(end_ms * frame_rate / 1000))
            total_frames = source.getnframes()
            start_frame = min(max(start_frame, 0), total_frames)
            end_frame = min(max(end_frame, start_frame), total_frames)
            source.setpos(start_frame)
            frames = source.readframes(end_frame - start_frame)
            with wave.open(str(output_file), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(16000)
                target.setcomptype("NONE", "not compressed")
                target.writeframes(frames)
    except (OSError, wave.Error) as exc:
        raise RuntimeError(f"failed to slice normalized VAD WAV: {exc}") from exc


def _clean_sensevoice_text(text: str) -> str:
    """Remove SenseVoice control tags while preserving the spoken content."""
    text = re.sub(r"<\|[^|]+?\|>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _generate_sensevoice_segment_results(
    sensevoice_model: object,
    segment_paths: list[Path],
    language: str,
) -> tuple[list[object], str]:
    """Use FunASR native dynamic batching, with a compatible sequential fallback."""
    generate_kwargs: dict[str, object] = {
        "language": language,
        "use_itn": True,
        # Each VAD segment is capped at 10 seconds, so six items stay within
        # the existing 60-second dynamic batch budget.
        "batch_size": DEFAULT_SENSEVOICE_SEGMENT_BATCH_SIZE,
        "batch_size_s": 60,
        "sentence_timestamp": True,
    }
    try:
        batched = sensevoice_model.generate(
            input=[str(path) for path in segment_paths],
            **generate_kwargs,
        )
        if not isinstance(batched, list) or len(batched) != len(segment_paths):
            raise RuntimeError(
                "SenseVoice batch result count does not match VAD segment count"
            )
        return list(batched), "dynamic_batch"
    except Exception as batch_exc:  # noqa: BLE001
        results: list[object] = []
        try:
            for path in segment_paths:
                results.append(
                    sensevoice_model.generate(input=str(path), **generate_kwargs)
                )
        except Exception as sequential_exc:  # noqa: BLE001
            raise RuntimeError(
                "SenseVoice batch and sequential segment inference both failed: "
                f"batch={batch_exc}; sequential={sequential_exc}"
            ) from sequential_exc
        return results, "sequential_fallback"


def _extract_model_text(result: object) -> str:
    chunks = result if isinstance(result, list) else [result]
    texts: list[str] = []
    for chunk in chunks:
        if isinstance(chunk, dict):
            text = str(chunk.get("text", ""))
        else:
            text = str(chunk)
        cleaned = _clean_sensevoice_text(text)
        if cleaned:
            texts.append(cleaned)
    return "\n".join(texts).strip()


def _ms_to_timestamp(value: object) -> str:
    try:
        ms = int(float(value))
    except (TypeError, ValueError):
        return ""
    total_seconds, millis = divmod(max(ms, 0), 1000)
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def _timestamp_pair_ms(value: object) -> tuple[object, object] | None:
    if isinstance(value, dict):
        start = value.get("start", value.get("start_ms", value.get("begin")))
        end = value.get("end", value.get("end_ms", value.get("finish")))
        if start is not None and end is not None:
            return start, end
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return value[0], value[1]
    return None


def _timestamp_index_has_usable_time(index: list[dict[str, object]]) -> bool:
    for item in index:
        if str(item.get("start_ms") or "").strip() and str(item.get("end_ms") or "").strip():
            return True
        if str(item.get("start") or "").strip() and str(item.get("end") or "").strip():
            return True
    return False


def _select_device(requested: str = "auto") -> str:
    requested = (requested or "auto").strip().lower()
    if requested and requested != "auto":
        return requested
    try:
        import torch  # type: ignore

        if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def _run_sensevoice_vad_segments(
    input_file: Path,
    sensevoice_model: object,
    vad_model: str,
    language: str,
    allow_remote_model_lookup: bool,
    keep_raw: bool = False,
) -> dict[str, object]:
    from funasr import AutoModel  # type: ignore

    try:
        vad_ref = _resolve_model_ref(vad_model, allow_remote_model_lookup=allow_remote_model_lookup)
        vad = AutoModel(
            model=vad_ref,
            trust_remote_code=True,
            device="cpu",
            disable_update=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise SenseVoiceSegmentPreparationError(f"VAD 初始化失败: {exc}") from exc
    temp_dir = tempfile.TemporaryDirectory(prefix="sensevoice-vad-segments-")
    segment_dir = Path(temp_dir.name)
    records: list[dict[str, object]] = []
    raw_results: list[dict[str, object]] = []
    try:
        try:
            # Normalize once so VAD and every segment use exactly the same PCM
            # timeline. Segment files are then copied with stdlib wave below.
            normalized_path = segment_dir / "normalized.wav"
            _normalize_audio_to_wav(input_file, normalized_path)
            vad_result = vad.generate(input=str(normalized_path))
            raw_segments = _extract_vad_segments(vad_result)
            if not raw_segments:
                raise RuntimeError("完整音频 VAD 未返回有效 segment")
            segments = _split_vad_segments_for_reliable_timestamps(raw_segments)
            if not segments:
                raise RuntimeError("完整音频 VAD 未返回可用于可靠时间戳的 segment")

            segment_paths: list[Path] = []
            for segment_index, (start_ms, end_ms) in enumerate(segments):
                segment_path = segment_dir / f"segment_{segment_index:05d}.wav"
                _audio_segment_to_wav(normalized_path, segment_path, start_ms, end_ms)
                segment_paths.append(segment_path)
        except Exception as exc:  # noqa: BLE001
            raise SenseVoiceSegmentPreparationError(f"VAD/音频预处理失败: {exc}") from exc

        # Once this starts, a segment inference failure is terminal. Re-running
        # the whole recording in 60-second chunks would duplicate model work.
        segment_results, inference_mode = _generate_sensevoice_segment_results(
            sensevoice_model,
            segment_paths,
            language,
        )
        for segment_index, ((start_ms, end_ms), segment_path, segment_result) in enumerate(
            zip(segments, segment_paths, segment_results)
        ):
            text = _extract_model_text(segment_result)
            if keep_raw:
                raw_results.append(
                    {
                        "segment_index": segment_index,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "duration_ms": end_ms - start_ms,
                        "file": str(segment_path),
                        "result": segment_result,
                    }
                )
            if not text:
                continue
            records.append(
                {
                    "start": _ms_to_timestamp(start_ms),
                    "end": _ms_to_timestamp(end_ms),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "duration_ms": end_ms - start_ms,
                    "chunk_index": segment_index,
                    "text": text,
                    "speaker": "",
                    "source": "sensevoice_vad_segment",
                    "precision": "segment",
                    "index": segment_index,
                }
            )
    finally:
        temp_dir.cleanup()

    if not records:
        raise RuntimeError("SenseVoice VAD segment 转写未返回有效文本")
    payload: dict[str, object] = {
        "ok": True,
        "text": "\n".join(str(item.get("text") or "").strip() for item in records if item.get("text")).strip(),
        "timestamp_index": records,
        "sentence_info": records,
        "raw": raw_results,
        "vad_segment_count": len(raw_segments),
        "sensevoice_segment_count": len(segments),
        "sensevoice_inference_mode": inference_mode,
    }
    if keep_raw:
        payload["vad_result"] = vad_result
    return payload


def _run_sensevoice(
    input_file: Path,
    output_dir: Path,
    model_name: str,
    language: str,
    output_format: str,
    allow_remote_model_lookup: bool,
    include_raw_json: bool,
) -> int:
    if output_format not in SENSEVOICE_TEXT_FORMATS:
        print(
            "SenseVoice 当前只允许输出 txt/json/all；禁止降级为 Whisper 生成字幕格式。",
            file=sys.stderr,
        )
        return 2

    output_dir.mkdir(parents=True, exist_ok=True)
    with _sensevoice_stem_lock(output_dir, input_file.stem):
        _recover_sensevoice_transactions(output_dir, input_file.stem)
        return _run_sensevoice_locked(
            input_file=input_file,
            output_dir=output_dir,
            model_name=model_name,
            language=language,
            output_format=output_format,
            allow_remote_model_lookup=allow_remote_model_lookup,
            include_raw_json=include_raw_json,
        )


def _run_sensevoice_locked(
    input_file: Path,
    output_dir: Path,
    model_name: str,
    language: str,
    output_format: str,
    allow_remote_model_lookup: bool,
    include_raw_json: bool,
) -> int:

    _ensure_ffmpeg_for_current_process()

    try:
        from funasr import AutoModel  # type: ignore
    except Exception as exc:
        print(f"缺少 SenseVoice 运行依赖: {exc}", file=sys.stderr)
        return 127

    base_model_kwargs: dict[str, object] = {
        "model": _resolve_model_ref(model_name, allow_remote_model_lookup=allow_remote_model_lookup),
        "trust_remote_code": True,
        "device": _select_device("auto"),
        "disable_update": True,
    }
    try:
        model = AutoModel(**base_model_kwargs)
    except (RuntimeError, TypeError, ValueError) as exc:
        if model_name == DEFAULT_SENSEVOICE_MODEL and "not registered" in str(exc):
            fallback_kwargs = dict(base_model_kwargs)
            fallback_kwargs["model"] = "SenseVoiceSmall"
            model = AutoModel(**fallback_kwargs)
        else:
            raise

    generate_kwargs: dict[str, object] = {
        "language": language,
        "use_itn": True,
        "batch_size_s": 60,
        "sentence_timestamp": True,
    }
    result: list[dict[str, object]] = []
    speaker_sentences: list[dict[str, object]] = []
    output_text = ""
    sensevoice_timestamp_index: list[dict[str, object]] = []
    timestamp_index_source = ""
    sensevoice_vad_status = ""
    sensevoice_inference_mode = ""
    try:
        vad_payload = _run_sensevoice_vad_segments(
            input_file=input_file,
            sensevoice_model=model,
            vad_model=DEFAULT_VAD_MODEL,
            language=language,
            allow_remote_model_lookup=allow_remote_model_lookup,
            keep_raw=include_raw_json,
        )
        output_text = str(vad_payload.get("text") or "").strip()
        speaker_sentences = (
            vad_payload.get("sentence_info") if isinstance(vad_payload.get("sentence_info"), list) else []
        )
        sensevoice_timestamp_index = (
            vad_payload.get("timestamp_index") if isinstance(vad_payload.get("timestamp_index"), list) else []
        )
        raw_results = vad_payload.get("raw") if isinstance(vad_payload.get("raw"), list) else []
        result.extend(raw_results)
        timestamp_index_source = "sensevoice_vad_segment"
        sensevoice_inference_mode = str(vad_payload.get("sensevoice_inference_mode") or "")
        sensevoice_vad_status = "完整音频 VAD segment + SenseVoice 分段转写完成。"
    except SenseVoiceSegmentPreparationError as exc:
        sensevoice_vad_status = f"SenseVoice segment 推理前的 VAD/音频预处理未完成，已转入 60 秒纯文本兜底: {exc}"
        print(sensevoice_vad_status, file=sys.stderr)
        chunk_temp_dir, chunks = _audio_file_chunks(input_file)
        chunk_texts: list[str] = []
        try:
            for chunk_index, chunk_path in enumerate(chunks):
                chunk_kwargs = dict(generate_kwargs)
                chunk_kwargs["input"] = str(chunk_path)
                chunk_result = model.generate(**chunk_kwargs)
                if include_raw_json:
                    result.append(
                        {
                            "chunk_index": chunk_index,
                            "file": str(chunk_path),
                            "result": chunk_result,
                        }
                    )
                chunk_text = _extract_model_text(chunk_result)
                if chunk_text:
                    chunk_texts.append(chunk_text)
        finally:
            chunk_temp_dir.cleanup()
        output_text = "\n".join(chunk_texts).strip()
    except Exception as exc:  # noqa: BLE001
        print(
            "SenseVoice segment 推理失败；为避免整场音频重复转写，本次不再进入 60 秒兜底: "
            f"{exc}",
            file=sys.stderr,
        )
        return 1
    if not output_text:
        print("SenseVoice 主转写未返回有效文本。", file=sys.stderr)
        return 1

    stem = input_file.stem
    timestamp_index = sensevoice_timestamp_index

    rendered_outputs: dict[str, str] = {}
    if output_format in {"txt", "all"}:
        rendered_outputs[".txt"] = output_text + "\n"
    if timestamp_index:
        rendered_outputs[".timestamp_index.json"] = json.dumps(
            timestamp_index,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    if output_format in {"json", "all"}:
        payload = {
            "engine": "sensevoice",
            "model": model_name,
            "language": language,
            "input": str(input_file),
            "text": output_text,
            "model_cache": _model_cache_report(),
            "timestamp_detected": _timestamp_index_has_usable_time(timestamp_index),
            "speakers": sorted({str(item.get("speaker")) for item in speaker_sentences if item.get("speaker")}),
            "sentence_info": speaker_sentences,
            "timestamp_index": timestamp_index,
            "timestamp_index_path": str(output_dir / f"{stem}.timestamp_index.json") if timestamp_index else "",
            "timestamp_index_source": timestamp_index_source,
            "sensevoice_timestamp_index": sensevoice_timestamp_index,
            "sensevoice_vad_status": sensevoice_vad_status,
            "sensevoice_inference_mode": sensevoice_inference_mode,
        }
        if include_raw_json:
            payload["raw"] = result
        rendered_outputs[".json"] = json.dumps(payload, ensure_ascii=False, indent=2, default=str)

    try:
        _commit_sensevoice_outputs(output_dir, stem, rendered_outputs)
    except OSError as exc:
        print(f"SenseVoice 输出提交失败，已保留上一轮完整结果: {exc}", file=sys.stderr)
        return 1

    print(f"SenseVoice 转录完成: {output_dir / f'{stem}.txt'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="使用本地 SenseVoice 转录音频；禁止降级为 Whisper")
    parser.add_argument("input_file", nargs="?", help="音频文件路径")
    parser.add_argument(
        "--output-dir",
        default=".",
        help="转录文件输出目录，默认当前目录",
    )
    parser.add_argument(
        "--engine",
        default="auto",
        choices=["auto", "sensevoice"],
        help="兼容旧调用的主 ASR 引擎参数；主转写固定使用 SenseVoice",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_SENSEVOICE_MODEL,
        help="SenseVoice 模型名，默认 iic/SenseVoiceSmall",
    )
    parser.add_argument(
        "--cache-dir",
        default="",
        help="可选模型缓存根目录；也可用 SENSEVOICE_MODEL_CACHE 环境变量设置",
    )
    parser.add_argument(
        "--output-format",
        default="txt",
        choices=["txt", "json", "all"],
        help="输出格式，默认 txt",
    )
    parser.add_argument(
        "--language",
        default="zh",
        help="语言，默认 zh",
    )
    parser.add_argument(
        "--check-model-cache",
        action="store_true",
        help="只检查本地 ASR 模型缓存完整性，不执行转录",
    )
    parser.add_argument(
        "--allow-remote-model-lookup",
        action="store_true",
        help="允许在本地缓存缺失时使用远程模型名；默认关闭，避免每次整理会议纪要时重新下载模型",
    )
    parser.add_argument(
        "--debug-raw-json",
        action="store_true",
        help="调试时在 JSON 中额外保留模型原始返回；默认关闭以减少写盘体积",
    )
    args = parser.parse_args()

    _configure_model_cache(args.cache_dir)
    if args.check_model_cache:
        _print_model_cache_report()
        report = _model_cache_report()
        models = report.get("models", {})
        sensevoice = models.get("sensevoice") if isinstance(models, dict) else {}
        vad = models.get("vad") if isinstance(models, dict) else {}
        if (
            isinstance(sensevoice, dict)
            and sensevoice.get("complete")
            and isinstance(vad, dict)
            and vad.get("complete")
        ):
            return 0
        return 1

    if not args.input_file:
        print("输入文件不存在: 请提供音频文件路径，或使用 --check-model-cache 仅检查模型缓存。", file=sys.stderr)
        return 1

    input_file = Path(args.input_file).expanduser().resolve()
    if not input_file.exists():
        print(f"输入文件不存在: {input_file}", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.engine in {"auto", "sensevoice"}:
        return _run_sensevoice(
            input_file=input_file,
            output_dir=output_dir,
            model_name=args.model,
            language=args.language,
            output_format=args.output_format,
            allow_remote_model_lookup=args.allow_remote_model_lookup,
            include_raw_json=args.debug_raw_json,
        )

    print("未知 ASR 引擎；只能使用 SenseVoice。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
