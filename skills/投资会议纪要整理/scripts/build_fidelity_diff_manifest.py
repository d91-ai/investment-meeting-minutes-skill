#!/usr/bin/env python3
"""Build a deterministic, main-owned fidelity diff manifest.

This command compares only explicit source/draft spans supplied by the main
workflow.  It deliberately does not perform named-entity recognition, infer
speaker or Q&A structure, or return a semantic verdict.  Its output is a
lexical change inventory and a bounded set of review packets; a specialist
may review those packets later, but this script never writes the Markdown
draft or any other final meeting artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
MANIFEST_TYPE = "fidelity_diff_manifest"
ARTIFACT_OWNER = "Main Orchestrator"
GENERATION_MODE = "deterministic_main_owned_v1"
DEFAULT_MAX_PARALLEL = 3
MAX_PARALLEL = 8
SMALL_GROUP_LIMIT = 6
SMALL_CHAR_LIMIT = 8_000
MAX_SHARDS = 3

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

# These are intentionally small, fixed lexical vocabularies.  They identify
# observations for review; they do not classify a sentence as true/false.
NEGATION_TOKENS = (
    "并不",
    "并非",
    "不再",
    "不具备",
    "不得",
    "不能",
    "不会",
    "没有",
    "尚未",
    "从未",
    "未能",
    "不是",
    "无法",
    "否认",
    "未曾",
    "未",
    "无",
    "没",
    "不",
)
CONDITION_TOKENS = (
    "在……情况下",
    "在...情况下",
    "取决于",
    "除非",
    "前提是",
    "前提",
    "如果",
    "只有",
    "一旦",
    "若",
    "如",
    "当",
    "截至",
    "可能",
    "预计",
)

_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:\s?(?:%|％|万亿|亿元|万元|亿美元|美元|千|百|十|亿|万|元|台|个|家|人|件|次|年|月|日|小时|分钟|秒|度|吨|公里|套|款|项|倍|GB|MB|TB))?"
    r"(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_DATE_TIME_RE = re.compile(
    r"(?<!\d)(?:"
    r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
    r"|\d{4}年\d{1,2}月(?:\d{1,2}日)?"
    r"|\d{1,2}月\d{1,2}日"
    r"|\d{1,2}:\d{2}(?::\d{2})?"
    r"|\d{1,2}点(?:\d{1,2}分)?"
    r")(?!\d)"
)
_QA_MARKER_RE = re.compile(
    r"(?im)^[ \t]*(?:"
    r"Q(?:[ \t]*[-._]?[ \t]*\d+)?[ \t]*[:：]"
    r"|A(?:[ \t]*[-._]?[ \t]*\d+)?[ \t]*[:：]"
    r"|问(?:题)?[ \t]*[:：]"
    r"|答(?:案)?[ \t]*[:：]"
    r"|问题[ \t]*\d*[ \t]*[:：]"
    r"|回答[ \t]*\d*[ \t]*[:：]"
    r")[ \t]*"
)


class FidelityManifestError(ValueError):
    """Raised when an input cannot be safely bound to a diff manifest."""


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_utf8(path: Path, label: str) -> tuple[str, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FidelityManifestError(f"{label} is not readable: {exc}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise FidelityManifestError(f"{label} must be UTF-8 without BOM")
    try:
        return raw.decode("utf-8"), raw
    except UnicodeDecodeError as exc:
        raise FidelityManifestError(f"{label} is not valid UTF-8: {exc}") from exc


def _read_json(path: Path, label: str) -> tuple[Any, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise FidelityManifestError(f"{label} is not readable: {exc}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise FidelityManifestError(f"{label} must be UTF-8 without BOM")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FidelityManifestError(f"{label} is not UTF-8 JSON: {exc}") from exc
    return payload, raw


def _sha_value(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not _SHA256.fullmatch(text):
        raise FidelityManifestError(f"{label} must be a 64-character SHA-256")
    return text


def _maybe_sha(payload: Any, names: Iterable[str], label: str) -> str:
    if not isinstance(payload, dict):
        return ""
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return _sha_value(value, label)
    return ""


def _display(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def _identity(value: Any) -> str:
    return _display(value).casefold()


def _safe_text(value: Any, label: str, *, max_chars: int = 256) -> str:
    text = _display(value)
    if not text:
        raise FidelityManifestError(f"{label} must not be empty")
    if len(text) > max_chars:
        raise FidelityManifestError(f"{label} exceeds {max_chars} characters")
    return text


def _span_from(value: Any, label: str, text_length: int) -> dict[str, int]:
    if not isinstance(value, dict):
        raise FidelityManifestError(f"{label} must be an object with start/end")
    start_value = None
    end_value = None
    for key in ("start_char", "start", "start_offset", "begin"):
        if key in value:
            start_value = value[key]
            break
    for key in ("end_char", "end", "end_offset", "stop"):
        if key in value:
            end_value = value[key]
            break
    if (
        isinstance(start_value, bool)
        or isinstance(end_value, bool)
        or not isinstance(start_value, int)
        or not isinstance(end_value, int)
    ):
        raise FidelityManifestError(f"{label} start/end must be integer character offsets")
    if start_value < 0 or end_value <= start_value or end_value > text_length:
        raise FidelityManifestError(f"{label} is outside text bounds or empty")
    result = {"start_char": start_value, "end_char": end_value}
    nested_sha = _maybe_sha(value, ("sha256", "hash"), f"{label}.sha256")
    if nested_sha:
        result["declared_sha256"] = nested_sha  # validated after slicing
    return result


def _span_value(item: dict[str, Any], kind: str) -> Any:
    for key in (f"{kind}_span", f"{kind}Span", kind):
        value = item.get(key)
        if isinstance(value, dict):
            return value
    # Flat aliases are accepted for simple span maps.
    prefix = "source" if kind == "source" else "draft"
    return {
        "start_char": item.get(f"{prefix}_start_char", item.get(f"{prefix}_start")),
        "end_char": item.get(f"{prefix}_end_char", item.get(f"{prefix}_end")),
    }


def _span_entries(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        raise FidelityManifestError("span map must include source_sha256 and draft_sha256")
    if not isinstance(payload, dict):
        raise FidelityManifestError("span map top-level must be a JSON object")
    for key in ("spans", "segments", "mappings", "span_map", "entries", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    raise FidelityManifestError("span map must contain a spans/segments/mappings array")


def _extract_bound_hash(payload: dict[str, Any], side: str) -> str:
    names = (f"{side}_sha256", f"{side}_hash")
    direct = _maybe_sha(payload, names, f"span map {side}_sha256")
    if direct:
        return direct
    nested = payload.get(side)
    if isinstance(nested, dict):
        nested_sha = _maybe_sha(nested, ("sha256", "hash", "text_sha256"), f"span map {side}_sha256")
        if nested_sha:
            return nested_sha
    raise FidelityManifestError(f"span map must explicitly bind {side}_sha256")


def _validate_span_map(
    payload: Any,
    source: str,
    draft: str,
    source_sha256: str,
    draft_sha256: str,
    span_map_sha256: str,
) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(payload, dict):
        raise FidelityManifestError("span map top-level must be a JSON object")
    bound_source = _extract_bound_hash(payload, "source")
    bound_draft = _extract_bound_hash(payload, "draft")
    if bound_source != source_sha256:
        raise FidelityManifestError("span map source_sha256 does not match source text")
    if bound_draft != draft_sha256:
        raise FidelityManifestError("span map draft_sha256 does not match draft Markdown")
    entries = _span_entries(payload)
    if not entries and (source or draft):
        raise FidelityManifestError("non-empty source/draft requires explicit span mappings")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(entries, start=1):
        if not isinstance(raw, dict):
            raise FidelityManifestError(f"span map entry {index} must be an object")
        source_span = _span_from(_span_value(raw, "source"), f"span map entry {index}.source_span", len(source))
        draft_span = _span_from(_span_value(raw, "draft"), f"span map entry {index}.draft_span", len(draft))
        source_slice = source[source_span["start_char"] : source_span["end_char"]]
        draft_slice = draft[draft_span["start_char"] : draft_span["end_char"]]
        declared_source = source_span.pop("declared_sha256", "")
        declared_draft = draft_span.pop("declared_sha256", "")
        actual_source_span_sha = hashlib.sha256(source_slice.encode("utf-8")).hexdigest()
        actual_draft_span_sha = hashlib.sha256(draft_slice.encode("utf-8")).hexdigest()
        if declared_source and declared_source != actual_source_span_sha:
            raise FidelityManifestError(f"span map entry {index}.source_span.sha256 is stale")
        if declared_draft and declared_draft != actual_draft_span_sha:
            raise FidelityManifestError(f"span map entry {index}.draft_span.sha256 is stale")
        qa_value = raw.get("qa_group_id")
        if qa_value in (None, ""):
            qa_value = raw.get("qaGroupId")
        qa_group_id = _display(qa_value) if qa_value not in (None, "") else ""
        group_value = raw.get("group_id")
        if group_value in (None, ""):
            group_value = raw.get("turn_id") or raw.get("turnId")
        group_id = _display(group_value) if group_value not in (None, "") else ""
        risk_flags = raw.get("risk_flags", raw.get("risks", []))
        if risk_flags in (None, ""):
            risk_flags = []
        if isinstance(risk_flags, str):
            risk_flags = [risk_flags]
        if not isinstance(risk_flags, list) or any(not isinstance(item, str) for item in risk_flags):
            raise FidelityManifestError(f"span map entry {index}.risk_flags must be a string array")
        span_id = _display(raw.get("span_id") or raw.get("spanId") or f"span_{index:06d}")
        if not _SAFE_ID.fullmatch(span_id):
            raise FidelityManifestError(f"span map entry {index}.span_id is not a safe identifier")
        normalized.append(
            {
                "span_id": span_id,
                "ordinal": index,
                "source_span": source_span,
                "draft_span": draft_span,
                "source_span_sha256": actual_source_span_sha,
                "draft_span_sha256": actual_draft_span_sha,
                "qa_group_id": qa_group_id,
                "explicit_group_id": group_id,
                "risk_flags": sorted({_safe_text(item, f"span map entry {index}.risk_flag", max_chars=80) for item in risk_flags if _display(item)}),
            }
        )
    span_ids = [str(item["span_id"]) for item in normalized]
    if len(span_ids) != len(set(span_ids)):
        raise FidelityManifestError("span map span_id values must be unique")
    for side in ("source_span", "draft_span"):
        previous_end = -1
        previous_start = -1
        for item in sorted(normalized, key=lambda record: (record[side]["start_char"], record[side]["end_char"], record["ordinal"])):
            start = item[side]["start_char"]
            end = item[side]["end_char"]
            if start < previous_end:
                raise FidelityManifestError(f"span map {side} entries overlap")
            if start == previous_start and end == previous_end:
                raise FidelityManifestError(f"span map {side} contains duplicate spans")
            previous_start, previous_end = start, end
    return normalized, span_map_sha256


def _iter_anchor_strings(value: Any, *, depth: int = 0) -> Iterable[str]:
    if depth > 4:
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_anchor_strings(item, depth=depth + 1)
        return
    if not isinstance(value, dict):
        return
    # Explicit identity fields only.  Evidence, source text, and arbitrary
    # context fields are intentionally not traversed.
    fields = (
        "candidate_term",
        "term",
        "original_term",
        "name",
        "entity",
        "entity_name",
        "display_name",
        "alias",
        "aliases",
        "verification_term",
        "candidate",
        "company",
        "product",
    )
    for field in fields:
        if field in value:
            raw = value[field]
            if isinstance(raw, str):
                shown = _display(raw)
                if shown and len(shown) <= 160:
                    yield shown
            else:
                yield from _iter_anchor_strings(raw, depth=depth + 1)
    for field in ("candidates", "entities", "items", "results", "records", "confirmed_items", "unresolved_items"):
        nested = value.get(field)
        if isinstance(nested, (list, dict)):
            yield from _iter_anchor_strings(nested, depth=depth + 1)


def _load_anchors(paths: Iterable[Path]) -> list[str]:
    values: dict[str, str] = {}
    for path in paths:
        payload, _ = _read_json(path, f"entity input {path}")
        for shown in _iter_anchor_strings(payload):
            values.setdefault(_identity(shown), shown)
    return [values[key] for key in sorted(values)]


def _match_fixed_tokens(text: str, tokens: Iterable[str]) -> list[dict[str, Any]]:
    matches: list[tuple[int, int, str]] = []
    for token in sorted(tokens, key=lambda item: (-len(item), item)):
        start = 0
        while True:
            found = text.find(token, start)
            if found < 0:
                break
            matches.append((found, found + len(token), token))
            start = found + 1
    selected: list[dict[str, Any]] = []
    for start, end, token in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]), item[2])):
        if any(
            start < item["end_char"] and end > item["start_char"]
            for item in selected
        ):
            continue
        selected.append({"text": token, "start_char": start, "end_char": end})
    return selected


def _match_regex(text: str, pattern: re.Pattern[str]) -> list[dict[str, Any]]:
    return [
        {"text": match.group(0), "start_char": match.start(), "end_char": match.end()}
        for match in pattern.finditer(text)
    ]


def _match_entities(text: str, anchors: list[str]) -> list[dict[str, Any]]:
    folded = text.casefold()
    matches: list[tuple[int, int, str]] = []
    for anchor in sorted(anchors, key=lambda item: (-len(item), _identity(item), item)):
        needle = anchor.casefold()
        start = 0
        while needle:
            found = folded.find(needle, start)
            if found < 0:
                break
            matches.append((found, found + len(anchor), anchor))
            start = found + max(1, len(needle))
    selected: list[dict[str, Any]] = []
    for start, end, anchor in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]), item[2])):
        if any(
            start < item["end_char"] and end > item["start_char"]
            for item in selected
        ):
            continue
        selected.append({"text": text[start:end], "anchor": anchor, "start_char": start, "end_char": end})
    return selected


def _lexical_inventory(text: str, anchors: list[str]) -> dict[str, Any]:
    return {
        "numbers_percent_units": _match_regex(text, _NUMBER_RE),
        "negation_tokens": _match_fixed_tokens(text, NEGATION_TOKENS),
        "condition_tokens": _match_fixed_tokens(text, CONDITION_TOKENS),
        "dates_times": _match_regex(text, _DATE_TIME_RE),
        "entity_anchors": _match_entities(text, anchors),
        "qa_markers": _match_regex(text, _QA_MARKER_RE),
    }


def _inventory_signature(value: list[dict[str, Any]], *, entity: bool = False) -> list[str]:
    if entity:
        return sorted(_identity(item.get("anchor", item.get("text", ""))) for item in value)
    return sorted(_identity(item.get("text", "")) for item in value)


def _group_id(group_key: str) -> str:
    return "fidelity_group__" + hashlib.sha256(group_key.encode("utf-8")).hexdigest()[:16]


def _qa_group_value(record: dict[str, Any], source_text: str, draft_text: str) -> str:
    if record["qa_group_id"]:
        return record["qa_group_id"]
    # Explicit Q/A markers are evidence of a boundary, not a semantic
    # speaker/Q&A inference.  The full marker text is retained in inventories.
    source_markers = _match_regex(source_text, _QA_MARKER_RE)
    draft_markers = _match_regex(draft_text, _QA_MARKER_RE)
    if source_markers or draft_markers:
        return ""
    return ""


def _build_groups(
    entries: list[dict[str, Any]], source: str, draft: str, anchors: list[str]
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        key_value = entry["qa_group_id"] or entry["explicit_group_id"]
        if key_value:
            group_key = ("qa:" if entry["qa_group_id"] else "group:") + _identity(key_value)
        else:
            group_key = f"span:{entry['source_span']['start_char']}:{entry['source_span']['end_char']}"
        grouped.setdefault(group_key, []).append(entry)
    ordered = sorted(
        grouped.items(),
        key=lambda item: (
            min(entry["source_span"]["start_char"] for entry in item[1]),
            min(entry["draft_span"]["start_char"] for entry in item[1]),
            item[0],
        ),
    )
    groups: list[dict[str, Any]] = []
    reason_by_inventory = {
        "numbers_percent_units": "number_inventory_changed",
        "negation_tokens": "negation_inventory_changed",
        "condition_tokens": "condition_inventory_changed",
        "dates_times": "date_time_inventory_changed",
        "entity_anchors": "entity_anchor_inventory_changed",
        "qa_markers": "qa_boundary_marker_changed",
    }
    for ordinal, (group_key, raw_entries) in enumerate(ordered, start=1):
        raw_entries = sorted(raw_entries, key=lambda item: (item["source_span"]["start_char"], item["ordinal"]))
        source_spans = [entry["source_span"] for entry in raw_entries]
        draft_spans = [entry["draft_span"] for entry in sorted(raw_entries, key=lambda item: (item["draft_span"]["start_char"], item["ordinal"]))]
        source_text = "".join(source[span["start_char"] : span["end_char"]] for span in source_spans)
        draft_text = "".join(draft[span["start_char"] : span["end_char"]] for span in draft_spans)
        source_inventory = _lexical_inventory(source_text, anchors)
        draft_inventory = _lexical_inventory(draft_text, anchors)
        reasons: list[str] = []
        if source_text != draft_text:
            reasons.append("text_changed")
        if len(source_text) != len(draft_text):
            reasons.append("span_length_changed")
        for inventory_key, reason in reason_by_inventory.items():
            source_signature = _inventory_signature(source_inventory[inventory_key], entity=inventory_key == "entity_anchors")
            draft_signature = _inventory_signature(draft_inventory[inventory_key], entity=inventory_key == "entity_anchors")
            if source_signature != draft_signature:
                reasons.append(reason)
        explicit_risks = sorted({risk for entry in raw_entries for risk in entry["risk_flags"]})
        reasons.extend("declared_risk:" + _identity(risk) for risk in explicit_risks)
        reasons = sorted(set(reasons))
        source_start = min(span["start_char"] for span in source_spans)
        source_end = max(span["end_char"] for span in source_spans)
        draft_start = min(span["start_char"] for span in draft_spans)
        draft_end = max(span["end_char"] for span in draft_spans)
        group_status = "changed" if reasons else "unchanged"
        if explicit_risks or "qa_boundary_marker_changed" in reasons:
            group_status = "at_risk"
        group_id = _group_id(group_key)
        qa_group_id = next((entry["qa_group_id"] for entry in raw_entries if entry["qa_group_id"]), "")
        turn_id = next((entry["explicit_group_id"] for entry in raw_entries if entry["explicit_group_id"]), "")
        group = {
            "group_id": group_id,
            "group_index": ordinal,
            "group_key": group_key,
            "qa_group_id": qa_group_id,
            "turn_id": turn_id,
            "span_ids": [str(entry["span_id"]) for entry in raw_entries],
            "source_spans": source_spans,
            "draft_spans": draft_spans,
            "source_span_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            "draft_span_sha256": hashlib.sha256(draft_text.encode("utf-8")).hexdigest(),
            "source_char_count": len(source_text),
            "draft_char_count": len(draft_text),
            "source_range": {"start_char": source_start, "end_char": source_end},
            "draft_range": {"start_char": draft_start, "end_char": draft_end},
            "source_inventory": source_inventory,
            "draft_inventory": draft_inventory,
            "status": group_status,
            "reasons": reasons,
        }
        groups.append(group)
    return groups


def _group_weight(group: dict[str, Any]) -> int:
    lexical_count = sum(
        len(group["source_inventory"].get(key, [])) + len(group["draft_inventory"].get(key, []))
        for key in (
            "numbers_percent_units",
            "negation_tokens",
            "condition_tokens",
            "dates_times",
            "entity_anchors",
            "qa_markers",
        )
    )
    return max(1, int(group["source_char_count"]) + int(group["draft_char_count"]) + lexical_count * 32)


def _partition(groups: list[dict[str, Any]], max_parallel: int) -> list[list[dict[str, Any]]]:
    if not groups:
        return []
    total_chars = sum(int(group["source_char_count"]) + int(group["draft_char_count"]) for group in groups)
    if len(groups) <= SMALL_GROUP_LIMIT and total_chars <= SMALL_CHAR_LIMIT:
        return [groups]
    target_count = min(MAX_SHARDS, max_parallel, len(groups))
    if target_count <= 1:
        return [groups]
    # A deterministic two/three-way target based on the amount of text under
    # review.  Boundaries are selected by prefix weight and never split a group.
    target_count = min(target_count, max(2, min(MAX_SHARDS, math.ceil(total_chars / SMALL_CHAR_LIMIT))))
    weights = [_group_weight(group) for group in groups]
    prefix = [0]
    for weight in weights:
        prefix.append(prefix[-1] + weight)
    partitions: list[list[dict[str, Any]]] = []
    start = 0
    for shard_index in range(target_count):
        remaining_shards = target_count - shard_index
        if remaining_shards == 1:
            end = len(groups)
        else:
            minimum_end = start + 1
            maximum_end = len(groups) - (remaining_shards - 1)
            desired = prefix[-1] * (shard_index + 1) / target_count
            candidates = range(minimum_end, maximum_end + 1)
            end = min(candidates, key=lambda index: (abs(prefix[index] - desired), index))
        partitions.append(groups[start:end])
        start = end
    return partitions


def _build_shards(
    review_groups: list[dict[str, Any]],
    *,
    source_sha256: str,
    draft_sha256: str,
    span_map_sha256: str,
    manifest_sha256: str,
    run_id: str,
    max_parallel: int,
) -> list[dict[str, Any]]:
    shards: list[dict[str, Any]] = []
    for number, groups in enumerate(_partition(review_groups, max_parallel), start=1):
        shard_id = f"shard_{number:03d}"
        group_ids = [str(group["group_id"]) for group in groups]
        span_ids = [str(span_id) for group in groups for span_id in group.get("span_ids", [])]
        shard_core = {
            "shard_id": shard_id,
            "shard_number": number,
            "group_ids": group_ids,
            "group_count": len(group_ids),
            "span_ids": span_ids,
            "source_sha256": source_sha256,
            "draft_sha256": draft_sha256,
            "span_map_sha256": span_map_sha256,
            "manifest_sha256": manifest_sha256,
        }
        input_sha256 = canonical_sha256(shard_core)
        artifact_type = f"fidelity_review_shard__{shard_id}"
        task_prefix = _safe_text(run_id, "run_id", max_chars=128) if run_id else "fidelity_review"
        shards.append(
            {
                **shard_core,
                "artifact_type": artifact_type,
                "artifact_owner": ARTIFACT_OWNER,
                "dispatch_phase": "draft_review",
                "task_id": f"{task_prefix}:fidelity_review:{shard_id}",
                "coverage": {"group_ids": group_ids, "span_ids": span_ids, "complete": True, "overlap": False},
                "input_sha256": input_sha256,
                "shard_sha256": input_sha256,
            }
        )
    return shards


def build_fidelity_diff_manifest(
    source_path: Path,
    draft_path: Path,
    span_map_path: Path,
    *,
    entity_candidate_paths: Iterable[Path] = (),
    entity_report_paths: Iterable[Path] = (),
    doubtful_items_paths: Iterable[Path] = (),
    max_parallel: int = DEFAULT_MAX_PARALLEL,
) -> dict[str, Any]:
    """Build a lexical-only source/final inventory and bounded shard plan."""

    if isinstance(max_parallel, bool) or not isinstance(max_parallel, int) or not 1 <= max_parallel <= MAX_PARALLEL:
        raise FidelityManifestError(f"max_parallel must be an integer from 1 to {MAX_PARALLEL}")
    source_path = source_path.expanduser().resolve()
    draft_path = draft_path.expanduser().resolve()
    span_map_path = span_map_path.expanduser().resolve()
    if source_path == draft_path:
        raise FidelityManifestError("source and draft paths must be different")
    source, source_bytes = _read_utf8(source_path, "source text")
    draft, draft_bytes = _read_utf8(draft_path, "draft Markdown")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    draft_sha256 = hashlib.sha256(draft_bytes).hexdigest()
    span_payload, span_bytes = _read_json(span_map_path, "span map")
    span_map_sha256 = hashlib.sha256(span_bytes).hexdigest()
    entries, span_map_sha256 = _validate_span_map(
        span_payload,
        source,
        draft,
        source_sha256,
        draft_sha256,
        span_map_sha256,
    )
    anchors = _load_anchors([*entity_candidate_paths, *entity_report_paths, *doubtful_items_paths])
    groups = _build_groups(entries, source, draft, anchors)
    changed_group_ids = [str(group["group_id"]) for group in groups if group["status"] == "changed"]
    at_risk_group_ids = [str(group["group_id"]) for group in groups if group["status"] == "at_risk"]
    review_group_ids = [str(group["group_id"]) for group in groups if group["status"] in {"changed", "at_risk"}]
    review_groups = [group for group in groups if group["status"] in {"changed", "at_risk"}]
    run_id = ""
    if isinstance(span_payload, dict):
        run_id = _display(span_payload.get("run_id") or span_payload.get("workflow_run_id") or "")
    core: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": MANIFEST_TYPE,
        "artifact_owner": ARTIFACT_OWNER,
        "generation_mode": GENERATION_MODE,
        "run_id": run_id,
        "source_sha256": source_sha256,
        "draft_sha256": draft_sha256,
        "span_map_sha256": span_map_sha256,
        "source_char_count": len(source),
        "draft_char_count": len(draft),
        "span_count": len(entries),
        "group_count": len(groups),
        "groups": groups,
        "review_groups": review_groups,
        "changed_group_ids": changed_group_ids,
        "at_risk_group_ids": at_risk_group_ids,
        "review_group_ids": review_group_ids,
        "unchanged_group_ids": [str(group["group_id"]) for group in groups if group["status"] == "unchanged"],
        "semantic_review_required": bool(review_groups),
        "policy": {
            "max_parallel": max_parallel,
            "max_shards": MAX_SHARDS,
            "small_group_limit": SMALL_GROUP_LIMIT,
            "small_char_limit": SMALL_CHAR_LIMIT,
            "group_order": "source_char_start_contiguous",
            "qa_group_indivisible": True,
        },
        "entity_anchor_count": len(anchors),
        "entity_anchor_sha256": canonical_sha256(anchors),
    }
    manifest_sha256 = canonical_sha256(core)
    shards = _build_shards(
        review_groups,
        source_sha256=source_sha256,
        draft_sha256=draft_sha256,
        span_map_sha256=span_map_sha256,
        manifest_sha256=manifest_sha256,
        run_id=run_id,
        max_parallel=max_parallel,
    )
    artifact = {
        **core,
        "shards": shards,
        "shard_count": len(shards),
        "shard_artifact_types": [str(shard["artifact_type"]) for shard in shards],
        "manifest_sha256": manifest_sha256,
        "deterministic_hash": manifest_sha256,
    }
    return artifact


# Import-friendly aliases used by lightweight regression helpers and callers
# that follow the other MAS builders' naming convention.
build_diff_manifest = build_fidelity_diff_manifest
build_manifest = build_fidelity_diff_manifest


def _paths(values: Iterable[str]) -> list[Path]:
    return [Path(value) for value in values if str(value).strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a deterministic fidelity diff manifest")
    parser.add_argument("--source", "--source-path", required=True, help="UTF-8 source text path")
    parser.add_argument("--draft", "--draft-path", required=True, help="UTF-8 draft Markdown path")
    parser.add_argument("--span-map", required=True, help="Explicit source/draft span-map JSON")
    parser.add_argument("--entity-candidates", "--entity-candidate", action="append", default=[], help="Optional explicit entity candidate JSON (repeatable)")
    parser.add_argument("--entity-report", action="append", default=[], help="Optional explicit entity report JSON (repeatable)")
    parser.add_argument("--doubtful-items", "--doubtful", action="append", default=[], help="Optional doubtful-items JSON (repeatable)")
    parser.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL)
    parser.add_argument("--out", required=True, help="Manifest JSON output path")
    parser.add_argument("--json", action="store_true", help="Print a JSON result")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = Path(args.out).expanduser().resolve()
    try:
        source_path = Path(args.source).expanduser().resolve()
        draft_path = Path(args.draft).expanduser().resolve()
        if output_path in {source_path, draft_path}:
            raise FidelityManifestError("output path must not overwrite source or draft Markdown")
        manifest = build_fidelity_diff_manifest(
            source_path,
            draft_path,
            Path(args.span_map),
            entity_candidate_paths=_paths(args.entity_candidates),
            entity_report_paths=_paths(args.entity_report),
            doubtful_items_paths=_paths(args.doubtful_items),
            max_parallel=args.max_parallel,
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = output_path.with_name(f".{output_path.name}.tmp")
        temporary_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary_path, output_path)
        result = {
            "ok": True,
            "out": str(output_path),
            "source_sha256": manifest["source_sha256"],
            "draft_sha256": manifest["draft_sha256"],
            "group_count": manifest["group_count"],
            "review_group_count": len(manifest["review_group_ids"]),
            "shard_count": manifest["shard_count"],
            "manifest_sha256": manifest["manifest_sha256"],
        }
    except (OSError, FidelityManifestError, ValueError) as exc:
        try:
            output_path.with_name(f".{output_path.name}.tmp").unlink(missing_ok=True)
        except OSError:
            pass
        result = {"ok": False, "errors": [f"build fidelity diff manifest failed: {exc.__class__.__name__}: {exc}"]}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
