#!/usr/bin/env python3
"""High-quality local transcription wrapper for SenseVoice."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_SENSEVOICE_MODEL = "iic/SenseVoiceSmall"
DEFAULT_PARAFORMER_MODEL = "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"
DEFAULT_VAD_MODEL = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
DEFAULT_LOCAL_CACHE = Path.home() / "Documents/Codex/asr-model-cache"
DEFAULT_AUDIO_CHUNK_SECONDS = 60
MAX_RELIABLE_VAD_SEGMENT_MS = 10000
MODEL_ALIASES = {
    "iic/SenseVoiceSmall": ("modelscope", "models/iic/SenseVoiceSmall"),
    "SenseVoiceSmall": ("modelscope", "models/iic/SenseVoiceSmall"),
    DEFAULT_PARAFORMER_MODEL: (
        "modelscope",
        "models/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    ),
    "Paraformer-Large": (
        "modelscope",
        "models/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    ),
    "paraformer": (
        "modelscope",
        "models/iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
    ),
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
    DEFAULT_PARAFORMER_MODEL: ("config.yaml", "model.pt"),
    "Paraformer-Large": ("config.yaml", "model.pt"),
    "paraformer": ("config.yaml", "model.pt"),
    DEFAULT_VAD_MODEL: ("config.yaml", "model.pt"),
    "fsmn-vad": ("config.yaml", "model.pt"),
}
SENSEVOICE_TEXT_FORMATS = {"txt", "json", "all"}


def _configure_model_cache(cache_dir: str) -> None:
    cache_value = (
        cache_dir
        or os.environ.get("SENSEVOICE_MODEL_CACHE")
        or os.environ.get("FUNASR_MODEL_CACHE")
        or str(DEFAULT_LOCAL_CACHE)
    ).strip()
    root = Path(cache_value).expanduser().resolve()
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
            "paraformer": _model_cache_status(DEFAULT_PARAFORMER_MODEL),
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
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg unavailable for VAD segment slicing")
    duration_ms = end_ms - start_ms
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start_ms / 1000:.3f}",
        "-i",
        str(input_file),
        "-t",
        f"{duration_ms / 1000:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(output_file),
    ]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0 or not output_file.exists():
        error = (completed.stderr or completed.stdout or "ffmpeg did not produce VAD segment").strip()
        raise RuntimeError(error)


def _clean_sensevoice_text(text: str) -> str:
    """Remove SenseVoice control tags while preserving the spoken content."""
    text = re.sub(r"<\|[^|]+?\|>", "", text)
    return re.sub(r"\s+", " ", text).strip()


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


def _speaker_label(value: object) -> str:
    raw = str(value if value is not None else "").strip()
    if not raw:
        return "Speaker ?"
    if raw.lower().startswith("speaker") or raw.startswith("发言人"):
        return raw
    return f"Speaker {raw}"


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


def _split_sentence_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    start = 0
    for match in re.finditer(r"[^。！？!?；;\n]+[。！？!?；;]?", text):
        sentence = match.group(0).strip()
        if not sentence:
            continue
        spans.append((match.start(), match.end(), sentence))
        start = match.end()
    tail = text[start:].strip()
    if tail:
        spans.append((start, len(text), tail))
    return spans or [(0, len(text), text)] if text else []


def _offset_sentence_times(
    sentences: list[dict[str, object]],
    *,
    offset_ms: int,
    chunk_index: int,
) -> list[dict[str, object]]:
    adjusted: list[dict[str, object]] = []
    for item in sentences:
        copied = dict(item)
        for key in ("start_ms", "end_ms"):
            value = copied.get(key, "")
            if str(value).strip() == "":
                continue
            try:
                copied[key] = int(float(value)) + offset_ms
            except (TypeError, ValueError):
                copied[key] = value
        copied["start"] = _ms_to_timestamp(copied.get("start_ms", ""))
        copied["end"] = _ms_to_timestamp(copied.get("end_ms", ""))
        copied["chunk_index"] = chunk_index
        adjusted.append(copied)
    return adjusted


def _segment_from_time_range(
    *,
    text: str,
    start_ms: object,
    end_ms: object,
    speaker: object = "",
    source: str,
    precision: str | None = None,
) -> dict[str, object]:
    return {
        "speaker": _speaker_label(speaker) if str(speaker or "").strip() else "",
        "speaker_id": speaker if str(speaker or "").strip() else "",
        "start_ms": start_ms,
        "end_ms": end_ms,
        "start": _ms_to_timestamp(start_ms),
        "end": _ms_to_timestamp(end_ms),
        "text": text,
        "source": source,
        "precision": precision or _infer_timestamp_precision(source, start_ms, end_ms),
    }


def _infer_timestamp_precision(source: object, start_ms: object = "", end_ms: object = "") -> str:
    if str(start_ms).strip() == "" or str(end_ms).strip() == "":
        return "unavailable"
    normalized = str(source or "").strip().lower()
    if "sentence" in normalized:
        return "sentence"
    if "phrase" in normalized:
        return "phrase"
    if "chunk" in normalized:
        return "chunk"
    if "segment" in normalized:
        return "segment"
    return "sentence"


def _extract_sentence_info(result: object) -> list[dict[str, object]]:
    chunks = result if isinstance(result, list) else [result]
    sentences: list[dict[str, object]] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        chunk_sentences: list[dict[str, object]] = []
        for item in chunk.get("sentence_info") or []:
            if not isinstance(item, dict):
                continue
            text = _clean_sensevoice_text(str(item.get("text") or item.get("sentence") or ""))
            if not text:
                continue
            chunk_sentences.append(
                _segment_from_time_range(
                    text=text,
                    start_ms=item.get("start", ""),
                    end_ms=item.get("end", ""),
                    speaker=item.get("spk", ""),
                    source="sentence_info",
                    precision="sentence",
                )
            )
        if chunk_sentences:
            sentences.extend(chunk_sentences)
            continue

        text = _clean_sensevoice_text(str(chunk.get("text") or ""))
        timestamp = chunk.get("timestamp")
        timestamp_pairs = [
            pair
            for pair in (_timestamp_pair_ms(item) for item in timestamp or [])
            if pair is not None
        ]
        if text and timestamp_pairs:
            spans = _split_sentence_spans(text)
            if len(timestamp_pairs) >= max(len(text), spans[-1][1] if spans else 0):
                for start_idx, end_idx, sentence in spans:
                    start_ms, _ = timestamp_pairs[max(start_idx, 0)]
                    _, end_ms = timestamp_pairs[min(max(end_idx - 1, 0), len(timestamp_pairs) - 1)]
                    sentences.append(
                        _segment_from_time_range(
                            text=sentence,
                            start_ms=start_ms,
                            end_ms=end_ms,
                            source="timestamp_sentence",
                            precision="sentence",
                        )
                    )
            else:
                start_ms, _ = timestamp_pairs[0]
                _, end_ms = timestamp_pairs[-1]
                sentences.append(
                    _segment_from_time_range(
                        text=text,
                        start_ms=start_ms,
                        end_ms=end_ms,
                        source="timestamp_segment",
                        precision="segment",
                    )
                )
            continue

        if text and chunk.get("start_ms") is not None and chunk.get("end_ms") is not None:
            sentences.append(
                _segment_from_time_range(
                    text=text,
                    start_ms=chunk.get("start_ms", ""),
                    end_ms=chunk.get("end_ms", ""),
                    source="chunk",
                    precision="chunk",
                )
            )
    return sentences


def _format_speaker_transcript(sentences: list[dict[str, object]]) -> str:
    lines: list[str] = []
    for item in sentences:
        speaker = str(item.get("speaker") or "").strip()
        start = str(item.get("start") or "")
        end = str(item.get("end") or "")
        time_range = f"[{start}-{end}] " if start or end else ""
        prefix = f"{speaker}: " if speaker else ""
        lines.append(f"{time_range}{prefix}{item.get('text', '')}".strip())
    return "\n".join(line for line in lines if line).strip()


def _build_timestamp_index(sentences: list[dict[str, object]], *, source_prefix: str = "sensevoice") -> list[dict[str, object]]:
    index: list[dict[str, object]] = []
    for offset, item in enumerate(sentences):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        start_ms = item.get("start_ms", "")
        end_ms = item.get("end_ms", "")
        source = str(item.get("source") or "sensevoice").strip() or "sensevoice"
        precision = str(item.get("precision") or _infer_timestamp_precision(source, start_ms, end_ms)).strip()
        index.append(
            {
                "start": item.get("start") or _ms_to_timestamp(start_ms),
                "end": item.get("end") or _ms_to_timestamp(end_ms),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "chunk_index": item.get("chunk_index", 0),
                "text": text,
                "speaker": item.get("speaker", ""),
                "source": source if source.startswith(source_prefix) else f"{source_prefix}_{source}",
                "precision": precision,
                "index": offset,
            }
        )
    return index


def _timestamp_index_has_usable_time(index: list[dict[str, object]]) -> bool:
    for item in index:
        if str(item.get("start_ms") or "").strip() and str(item.get("end_ms") or "").strip():
            return True
        if str(item.get("start") or "").strip() and str(item.get("end") or "").strip():
            return True
    return False


def _text_diff_preview(primary_text: str, auxiliary_text: str, *, limit: int = 120) -> str:
    primary_lines = [line.strip() for line in primary_text.splitlines() if line.strip()]
    auxiliary_lines = [line.strip() for line in auxiliary_text.splitlines() if line.strip()]
    if not primary_lines or not auxiliary_lines:
        return ""
    diff = list(
        difflib.unified_diff(
            primary_lines,
            auxiliary_lines,
            fromfile="sensevoice",
            tofile="paraformer",
            lineterm="",
            n=1,
        )
    )
    return "\n".join(diff[:limit]).strip()


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


def _run_paraformer_auxiliary(
    input_file: Path,
    model_name: str,
    allow_remote_model_lookup: bool,
) -> dict[str, object]:
    try:
        from funasr import AutoModel  # type: ignore
    except Exception as exc:
        return {
            "engine": "paraformer",
            "model": model_name,
            "ok": False,
            "text": "",
            "status": f"缺少 Paraformer 运行依赖: {exc}",
        }

    try:
        model_ref = _resolve_model_ref(model_name, allow_remote_model_lookup=allow_remote_model_lookup)
        model = AutoModel(
            model=model_ref,
            trust_remote_code=True,
            device=_select_device("cpu"),
            disable_update=True,
        )
        chunk_temp_dir, chunks = _audio_file_chunks(input_file)
        chunk_texts: list[str] = []
        sentence_info: list[dict[str, object]] = []
        try:
            for chunk_index, chunk_path in enumerate(chunks):
                result = model.generate(input=str(chunk_path), batch_size_s=60, sentence_timestamp=True)
                chunk_text = _extract_model_text(result)
                if chunk_text:
                    chunk_texts.append(chunk_text)
                sentence_info.extend(
                    _offset_sentence_times(
                        _extract_sentence_info(result),
                        offset_ms=chunk_index * DEFAULT_AUDIO_CHUNK_SECONDS * 1000,
                        chunk_index=chunk_index,
                    )
                )
        finally:
            chunk_temp_dir.cleanup()
        text = "\n".join(item for item in chunk_texts if item).strip()
        timestamp_index = _build_timestamp_index(sentence_info, source_prefix="paraformer")
    except Exception as exc:  # noqa: BLE001
        return {
            "engine": "paraformer",
            "model": model_name,
            "ok": False,
            "text": "",
            "status": f"Paraformer 辅助校验未完成: {exc}",
        }

    return {
        "engine": "paraformer",
        "model": model_name,
        "ok": True,
        "text": text,
        "timestamp_detected": _timestamp_index_has_usable_time(timestamp_index),
        "sentence_info": sentence_info,
        "timestamp_index": timestamp_index,
        "status": "Paraformer 辅助转写完成；仅作为校对和时间戳证据，不自动覆盖 SenseVoice 主转写。",
    }


def _run_sensevoice_vad_segments(
    input_file: Path,
    sensevoice_model: object,
    vad_model: str,
    language: str,
    allow_remote_model_lookup: bool,
) -> dict[str, object]:
    from funasr import AutoModel  # type: ignore

    vad_ref = _resolve_model_ref(vad_model, allow_remote_model_lookup=allow_remote_model_lookup)
    vad = AutoModel(
        model=vad_ref,
        trust_remote_code=True,
        device="cpu",
        disable_update=True,
    )
    vad_result = vad.generate(input=str(input_file))
    raw_segments = _extract_vad_segments(vad_result)
    if not raw_segments:
        raise RuntimeError("完整音频 VAD 未返回有效 segment")
    segments = _split_vad_segments_for_reliable_timestamps(raw_segments)
    if not segments:
        raise RuntimeError("完整音频 VAD 未返回可用于可靠时间戳的 segment")

    temp_dir = tempfile.TemporaryDirectory(prefix="sensevoice-vad-segments-")
    segment_dir = Path(temp_dir.name)
    records: list[dict[str, object]] = []
    raw_results: list[dict[str, object]] = []
    try:
        for segment_index, (start_ms, end_ms) in enumerate(segments):
            segment_path = segment_dir / f"segment_{segment_index:05d}.wav"
            _audio_segment_to_wav(input_file, segment_path, start_ms, end_ms)
            segment_result = sensevoice_model.generate(
                input=str(segment_path),
                language=language,
                use_itn=True,
                batch_size_s=60,
                sentence_timestamp=True,
            )
            text = _extract_model_text(segment_result)
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
    return {
        "ok": True,
        "text": "\n".join(str(item.get("text") or "").strip() for item in records if item.get("text")).strip(),
        "timestamp_index": records,
        "sentence_info": records,
        "raw": raw_results,
        "vad_result": vad_result,
        "vad_segment_count": len(raw_segments),
        "sensevoice_segment_count": len(segments),
    }


def _run_sensevoice(
    input_file: Path,
    output_dir: Path,
    model_name: str,
    language: str,
    output_format: str,
    allow_remote_model_lookup: bool,
    include_raw_json: bool,
    aux_engine: str,
    aux_model: str,
    aux_strict: bool,
) -> int:
    if output_format not in SENSEVOICE_TEXT_FORMATS:
        print(
            "SenseVoice 当前只允许输出 txt/json/all；禁止降级为 Whisper 生成字幕格式。",
            file=sys.stderr,
        )
        return 2

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
    try:
        vad_payload = _run_sensevoice_vad_segments(
            input_file=input_file,
            sensevoice_model=model,
            vad_model=DEFAULT_VAD_MODEL,
            language=language,
            allow_remote_model_lookup=allow_remote_model_lookup,
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
        sensevoice_vad_status = "完整音频 VAD segment + SenseVoice 分段转写完成。"
    except Exception as exc:  # noqa: BLE001
        sensevoice_vad_status = f"SenseVoice VAD segment 时间戳链路未完成，已转入 60 秒纯文本兜底: {exc}"
        print(sensevoice_vad_status, file=sys.stderr)
        chunk_temp_dir, chunks = _audio_file_chunks(input_file)
        chunk_texts: list[str] = []
        try:
            for chunk_index, chunk_path in enumerate(chunks):
                chunk_kwargs = dict(generate_kwargs)
                chunk_kwargs["input"] = str(chunk_path)
                chunk_result = model.generate(**chunk_kwargs)
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
    auxiliary: dict[str, object] = {
        "engine": "",
        "model": "",
        "ok": False,
        "text": "",
        "status": "",
    }
    if aux_engine == "paraformer":
        auxiliary = _run_paraformer_auxiliary(
            input_file=input_file,
            model_name=aux_model,
            allow_remote_model_lookup=allow_remote_model_lookup,
        )
        if aux_strict and not auxiliary.get("ok"):
            print(str(auxiliary.get("status") or "Paraformer auxiliary transcription failed"), file=sys.stderr)
            return 1

    stem = input_file.stem
    paraformer_timestamp_index = (
        auxiliary.get("timestamp_index") if isinstance(auxiliary.get("timestamp_index"), list) else []
    )
    timestamp_index = sensevoice_timestamp_index
    if (
        not timestamp_index
        and paraformer_timestamp_index
        and _timestamp_index_has_usable_time(paraformer_timestamp_index)
    ):
        timestamp_index = paraformer_timestamp_index
        timestamp_index_source = "paraformer"

    if output_format in {"txt", "all"}:
        (output_dir / f"{stem}.txt").write_text(output_text + "\n", encoding="utf-8")
        if auxiliary.get("ok") and auxiliary.get("text"):
            (output_dir / f"{stem}.paraformer.txt").write_text(
                str(auxiliary.get("text")).strip() + "\n",
                encoding="utf-8",
            )
        if paraformer_timestamp_index:
            (output_dir / f"{stem}.paraformer.timestamp_index.json").write_text(
                json.dumps(paraformer_timestamp_index, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
    if timestamp_index:
        (output_dir / f"{stem}.timestamp_index.json").write_text(
            json.dumps(timestamp_index, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
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
            "paraformer_timestamp_detected": bool(auxiliary.get("timestamp_detected")),
            "paraformer_timestamp_index": paraformer_timestamp_index,
            "auxiliary_engine": auxiliary.get("engine") or "",
            "auxiliary_model": auxiliary.get("model") or "",
            "auxiliary_text": auxiliary.get("text") or "",
            "auxiliary_ok": bool(auxiliary.get("ok")),
            "auxiliary_status": auxiliary.get("status") or "",
            "asr_comparison_diff": _text_diff_preview(output_text, str(auxiliary.get("text") or ""))
            if auxiliary.get("ok")
            else "",
        }
        if include_raw_json:
            payload["raw"] = result
        (output_dir / f"{stem}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

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
        "--aux-engine",
        default="paraformer",
        choices=["none", "paraformer"],
        help="辅助校对 ASR；默认 paraformer，仅作为校对证据，不替换 SenseVoice 主转写",
    )
    parser.add_argument(
        "--aux-model",
        default=DEFAULT_PARAFORMER_MODEL,
        help="Paraformer 辅助模型名",
    )
    parser.add_argument(
        "--aux-strict",
        action="store_true",
        help="Paraformer 辅助校验失败时让本次转写失败；默认只记录失败状态并保留 SenseVoice 主结果",
    )
    parser.add_argument(
        "--no-aux",
        action="store_true",
        help="禁用辅助 ASR 校对，只运行 SenseVoice",
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
        paraformer = models.get("paraformer") if isinstance(models, dict) else {}
        vad = models.get("vad") if isinstance(models, dict) else {}
        if (
            isinstance(sensevoice, dict)
            and sensevoice.get("complete")
            and isinstance(paraformer, dict)
            and paraformer.get("complete")
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
            aux_engine="none" if args.no_aux else args.aux_engine,
            aux_model=args.aux_model,
            aux_strict=args.aux_strict,
        )

    print("未知 ASR 引擎；只能使用 SenseVoice。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
