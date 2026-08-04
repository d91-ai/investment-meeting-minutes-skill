#!/usr/bin/env python3
"""Build a deterministic, privacy-bounded entity verification manifest.

The main workflow owns this manifest.  It is intentionally independent from
the network verifier: candidate discovery may use private source material,
but the verification packets emitted here contain only the minimum public
terms needed by a verifier.  Related candidates are kept in the same group so
that a company/code/product/customer relationship is never split across
verification shards.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
DEFAULT_MAX_PARALLEL = 3
DEFAULT_SHARD_TARGET_WEIGHT = 112
DEFAULT_MAX_ENTITY_WAVES = 2
MAX_SHARD_TARGET_WEIGHT = 1_000_000
MAX_ENTITY_WAVES = 64
DIRECT_CANDIDATE_LIMIT = 12
DIRECT_WEIGHT_LIMIT = 16
DIRECT_HIGH_RISK_LIMIT = 4
ALLOWED_MODES = {"auto", "single", "parallel"}
KNOWN_RELATION_KINDS = {
    "company_code",
    "product_company",
    "customer_supplier",
}
ALLOWED_RELATION_KINDS = {"company_code"}
ALLOWED_VERIFICATION_KINDS = {
    "brand_company",
    "company",
    "company_code",
    "company_identity",
    "entity_identity",
    "industry",
    "industry_term",
    "security",
    "security_code",
    "stock",
    "stock_code",
    "term",
    "term_identity",
    "terminology",
}
TERM_KIND_ALIASES = {
    "model": "term_identity",
    "product": "term_identity",
    "product_program": "term_identity",
}
ENTITY_ROLE_KIND_ALIASES = {
    "competitor": "company_identity",
    "customer": "company_identity",
    "supplier": "company_identity",
}
EXCLUDED_MEETING_FACT_KINDS = {
    "date",
    "dates",
    "number",
    "numbers",
    "customer_supplier",
    "numbers_dates",
    "product_company",
    "time",
    "times",
}
RISK_LEVELS = {"low": 1, "medium": 2, "high": 4}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
ALLOWED_NETWORK_VERIFICATION_REASONS = {
    "abbreviation_ambiguous",
    "confirmed_code_required",
    "local_multiple_candidates",
    "local_not_found",
    "source_conflict",
    "source_identity_unclear",
}


def canonical_sha256(value: Any) -> str:
    """Return the SHA-256 of canonical UTF-8 JSON."""

    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def canonical_json_digest(value: Any) -> str:
    """Compatibility alias used by the other MAS builders."""

    return canonical_sha256(value)


def normalize_text(value: Any) -> str:
    """NFKC, whitespace, and case-fold normalization used for identity."""

    text = unicodedata.normalize("NFKC", str(value))
    # ``split`` treats all Unicode whitespace as separators and avoids
    # preserving private source formatting in identity keys.
    return " ".join(text.split()).casefold()


def display_text(value: Any) -> str:
    """NFKC/whitespace-normalized display text without changing case."""

    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def _looks_like_private_path(value: str) -> bool:
    """Fail closed for values that could disclose a local/private location."""

    return (
        value.startswith(("/", "~/", "file://", "private/", "Users/", "home/"))
        or bool(_SCHEME.match(value))
        or bool(re.match(r"^[A-Za-z]:[\\/]", value))
    )


def _as_list(value: Any, field: str) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return [value]
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是 string 或 JSON array")
    return list(value)


def _normalized_display_list(
    value: Any,
    field: str,
    *,
    max_items: int = 64,
    max_chars: int = 160,
) -> list[str]:
    """Normalize a small public list while preserving a deterministic display."""

    values = _as_list(value, field)
    if len(values) > max_items:
        raise ValueError(f"{field} 不得超过 {max_items} 项")
    by_key: dict[str, str] = {}
    for item in values:
        if isinstance(item, (dict, list, tuple, set)) or isinstance(item, bool):
            raise ValueError(f"{field} 的每一项必须是 scalar string")
        shown = display_text(item)
        key = normalize_text(shown)
        if not key:
            continue
        if len(shown) > max_chars:
            raise ValueError(f"{field} 单项不得超过 {max_chars} 字符")
        previous = by_key.get(key)
        if previous is None or (shown, key) < (previous, key):
            by_key[key] = shown
    return [by_key[key] for key in sorted(by_key)]


def _pick_term(candidate: dict[str, Any]) -> str:
    for key in ("candidate_term", "term", "original_term", "name"):
        value = candidate.get(key)
        if value is not None and display_text(value):
            shown = display_text(value)
            if len(shown) > 160:
                raise ValueError(f"候选词 {key} 不得超过 160 字符")
            return shown
    raise ValueError("候选必须包含非空 candidate_term/term/original_term/name")


def _safe_candidate_id(value: Any, identity_key: str) -> str:
    shown = display_text(value) if value not in (None, "") else ""
    if shown and _SAFE_ID.fullmatch(shown):
        return shown
    return f"candidate_{hashlib.sha256(identity_key.encode('utf-8')).hexdigest()[:12]}"


def _risk_level(value: Any) -> str:
    level = normalize_text(value or "low")
    if level not in RISK_LEVELS:
        raise ValueError("risk_level 必须是 low、medium 或 high")
    return level


def _relation_kinds(candidate: dict[str, Any]) -> list[str]:
    values = _as_list(candidate.get("relation_kind"), "relation_kind")
    values.extend(_as_list(candidate.get("relation_kinds"), "relation_kinds"))
    kinds = sorted({normalize_text(item) for item in values if normalize_text(item)})
    unknown = sorted(set(kinds) - KNOWN_RELATION_KINDS)
    if unknown:
        raise ValueError("不支持的 relation_kind: " + ", ".join(unknown))
    return [kind for kind in kinds if kind in ALLOWED_RELATION_KINDS]


def _scoped_verification_kinds(values: list[str]) -> tuple[list[str], list[str]]:
    """Keep only public identity/term checks; never turn meeting claims into web checks."""

    included: set[str] = set()
    excluded: set[str] = set()
    for value in values:
        kind = normalize_text(value)
        if kind in ALLOWED_VERIFICATION_KINDS:
            included.add(kind)
        elif kind in TERM_KIND_ALIASES:
            included.add(TERM_KIND_ALIASES[kind])
        elif kind in ENTITY_ROLE_KIND_ALIASES:
            included.add(ENTITY_ROLE_KIND_ALIASES[kind])
        elif kind in EXCLUDED_MEETING_FACT_KINDS:
            excluded.add(kind)
        else:
            raise ValueError(f"不支持的 verification_kind: {kind}")
    return sorted(included), sorted(excluded)


def _relation_references(candidate: dict[str, Any], kinds: list[str]) -> list[dict[str, str]]:
    """Read explicit candidate links without ever copying their context text."""

    references: list[dict[str, str]] = []
    default_kind = kinds[0] if len(kinds) == 1 else ""
    for field in ("related_candidate_ids", "related_terms", "related_entities", "relations", "links"):
        for item in _as_list(candidate.get(field), field):
            ref: Any = item
            kind = default_kind
            if isinstance(item, dict):
                ref = (
                    item.get("candidate_id")
                    or item.get("related_candidate_id")
                    or item.get("candidate")
                    or item.get("term")
                    or item.get("related_term")
                    or item.get("name")
                )
                kind = normalize_text(item.get("relation_kind") or item.get("kind") or default_kind)
            if ref in (None, ""):
                continue
            shown = display_text(ref)
            if not shown or len(shown) > 160:
                raise ValueError(f"{field} 的引用必须是非空且不超过 160 字符")
            if kind not in ALLOWED_RELATION_KINDS:
                # A relation without a recognized kind is not allowed to
                # split/merge candidates implicitly.  relation_ids below are
                # still handled as explicit identifiers.
                continue
            references.append({"reference": shown, "key": normalize_text(shown), "relation_kind": kind})
    return references


def _candidate_weight(risk_level: str, verification_kinds: Iterable[str], relation_kinds: Iterable[str]) -> int:
    weight = RISK_LEVELS[risk_level]
    kinds = set(verification_kinds) | set(relation_kinds)
    if "company_code" in kinds or "customer_supplier" in kinds:
        weight += 1
    if "numbers_dates" in kinds:
        weight += 1
    return weight


def _needs_network_verification(
    raw: dict[str, Any],
    *,
    reason_codes: list[str],
    verification_kinds: list[str],
) -> bool:
    """Admit only candidates whose public identity is genuinely unresolved."""

    explicit = raw.get("network_verification_required")
    if explicit is not None and not isinstance(explicit, bool):
        raise ValueError("network_verification_required 必须是 boolean")
    if explicit is False and reason_codes:
        raise ValueError("network_verification_required=false 不得同时声明 verification_reason_codes")
    if explicit is True and not reason_codes:
        raise ValueError("network_verification_required=true 必须声明 verification_reason_codes")
    if "confirmed_code_required" in reason_codes and not set(verification_kinds) & {
        "company_code",
        "security_code",
        "stock_code",
    }:
        raise ValueError("confirmed_code_required 必须绑定代码核验 verification_kind")
    return explicit is not False and bool(reason_codes)


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, value: int) -> int:
        parent = self.parent[value]
        if parent != value:
            self.parent[value] = self.find(parent)
        return self.parent[value]

    def union(self, left: int, right: int) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root == right_root:
            return
        if self.rank[left_root] < self.rank[right_root]:
            left_root, right_root = right_root, left_root
        self.parent[right_root] = left_root
        if self.rank[left_root] == self.rank[right_root]:
            self.rank[left_root] += 1


def _source_hash(payload: dict[str, Any], candidates: list[Any]) -> tuple[str, str]:
    value = payload.get("source_sha256") or payload.get("source_hash")
    if value:
        digest = normalize_text(value)
        if not _SHA256.fullmatch(digest):
            raise ValueError("source_sha256 必须是 64 位小写 SHA-256")
        return digest, "provided"
    # A caller that has no source bytes can still bind the candidate manifest
    # deterministically.  This is explicitly labelled derived, not presented
    # as a hash of the private transcript.
    # Sort by each candidate's canonical digest so an extractor changing only
    # discovery order does not change the derived binding hash.
    stable_candidates = sorted(candidates, key=canonical_sha256)
    return canonical_sha256({"candidates": stable_candidates}), "derived_from_candidates"


def _input_candidates(payload: Any) -> tuple[dict[str, Any], list[Any]]:
    if isinstance(payload, list):
        return {}, payload
    if not isinstance(payload, dict):
        raise ValueError("输入 JSON 顶层必须是 object 或候选 array")
    candidates = payload.get("candidates")
    if candidates is None:
        candidates = payload.get("entities")
    if candidates is None:
        candidates = payload.get("items")
    if not isinstance(candidates, list):
        raise ValueError("输入必须包含 candidates JSON array")
    return payload, candidates


def _prepare_records(
    raw_candidates: list[Any],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any]]:
    by_term: dict[str, dict[str, Any]] = {}
    raw_id_to_term: dict[str, str] = {}
    excluded_kinds: set[str] = set()
    dropped_candidate_count = 0
    dropped_without_reason_candidate_count = 0
    admission_reason_counts = {reason: 0 for reason in sorted(ALLOWED_NETWORK_VERIFICATION_REASONS)}
    stripped_public_keyword_count = 0
    stripped_relation_candidate_count = 0
    for index, raw in enumerate(raw_candidates, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"candidates[{index}] 必须是 JSON object")
        term = _pick_term(raw)
        if _looks_like_private_path(term):
            raise ValueError(f"candidates[{index}] 候选词不得是 URL 或本地/私密路径")
        term_key = normalize_text(term)
        aliases = _normalized_display_list(raw.get("aliases"), f"candidates[{index}].aliases")
        raw_relation_ids = _normalized_display_list(
            raw.get("relation_ids"), f"candidates[{index}].relation_ids"
        )
        ambiguity = _normalized_display_list(
            raw.get("ambiguity_set"), f"candidates[{index}].ambiguity_set"
        )
        raw_verification_kinds = _normalized_display_list(
            raw.get("verification_kinds"), f"candidates[{index}].verification_kinds"
        )
        reason_codes = _normalized_display_list(
            raw.get("verification_reason_codes"),
            f"candidates[{index}].verification_reason_codes",
        )
        unknown_reason_codes = sorted(set(reason_codes) - ALLOWED_NETWORK_VERIFICATION_REASONS)
        if unknown_reason_codes:
            raise ValueError("不支持的 verification_reason_codes: " + ", ".join(unknown_reason_codes))
        verification_kinds, candidate_excluded_kinds = _scoped_verification_kinds(
            raw_verification_kinds
        )
        excluded_kinds.update(candidate_excluded_kinds)
        if not verification_kinds:
            dropped_candidate_count += 1
            continue
        relation_kinds = _relation_kinds(raw)
        relation_ids = raw_relation_ids if relation_kinds else []
        if raw_relation_ids and not relation_ids:
            stripped_relation_candidate_count += 1
        risk_value = raw.get("risk_level", raw.get("risk"))
        if risk_value in (None, "") and raw.get("risk_flags") not in (None, ""):
            risk_flags = {
                normalize_text(item)
                for item in _as_list(raw.get("risk_flags"), f"candidates[{index}].risk_flags")
            }
            if risk_flags & {"high", "high_risk", "high-risk"}:
                risk_value = "high"
            elif risk_flags & {"medium", "medium_risk", "medium-risk"}:
                risk_value = "medium"
        risk = _risk_level(risk_value)
        keywords = _normalized_display_list(
            raw.get("public_keywords"), f"candidates[{index}].public_keywords", max_chars=120
        )
        if keywords:
            stripped_public_keyword_count += len(keywords)
            keywords = []
        if candidate_excluded_kinds or (raw_relation_ids and not relation_ids):
            if not set(verification_kinds) & {
                "company_code",
                "security_code",
                "stock_code",
            }:
                # The removed relation/fact dimensions are the usual reason
                # these candidates were labelled high risk.  Identity-only
                # packets have comparable bounded work even when aliases are
                # present, so do not preserve the private-claim risk weight.
                risk = "low"
        if not _needs_network_verification(
            raw,
            reason_codes=reason_codes,
            verification_kinds=verification_kinds,
        ):
            dropped_candidate_count += 1
            dropped_without_reason_candidate_count += 1
            continue
        for reason_code in reason_codes:
            admission_reason_counts[reason_code] += 1
        if any(_looks_like_private_path(value) for value in [*aliases, *keywords]):
            raise ValueError(
                f"candidates[{index}] aliases/public_keywords 不得包含 URL 或本地/私密路径"
            )
        input_id = display_text(raw.get("candidate_id") or raw.get("id") or "")
        if input_id:
            input_id_key = normalize_text(input_id)
            previous_term = raw_id_to_term.get(input_id_key)
            if previous_term is not None and previous_term != term_key:
                raise ValueError(f"candidate_id {input_id} 绑定了多个不同候选词")
            raw_id_to_term[input_id_key] = term_key
        identity_key = canonical_sha256(
            {
                "term": term_key,
                "verification_kinds": sorted(set(verification_kinds)),
            }
        )
        record = by_term.get(term_key)
        if record is None:
            record = {
                "term": term,
                "term_key": term_key,
                "input_ids": [],
                "aliases": aliases,
                "alias_keys": {normalize_text(item) for item in aliases},
                "relation_ids": relation_ids,
                "relation_id_keys": {normalize_text(item) for item in relation_ids},
                "ambiguity_set": ambiguity,
                "ambiguity_keys": {normalize_text(item) for item in ambiguity},
                "verification_kinds": set(verification_kinds),
                "relation_kinds": set(relation_kinds),
                "public_keywords": keywords,
                "risk_level": risk,
                "references": _relation_references(raw, relation_kinds),
                "identity_key": identity_key,
                "verification_reason_codes": set(reason_codes),
            }
            by_term[term_key] = record
        else:
            # Exact normalized-term duplicates collapse before relation
            # grouping.  Merge metadata deterministically and never double the
            # candidate's risk weight.
            record["aliases"] = _merge_display_values(record["aliases"], aliases)
            record["alias_keys"].update(normalize_text(item) for item in aliases)
            record["relation_ids"] = _merge_display_values(record["relation_ids"], relation_ids)
            record["relation_id_keys"].update(normalize_text(item) for item in relation_ids)
            record["ambiguity_set"] = _merge_display_values(record["ambiguity_set"], ambiguity)
            record["ambiguity_keys"].update(normalize_text(item) for item in ambiguity)
            record["verification_kinds"].update(verification_kinds)
            record["verification_reason_codes"].update(reason_codes)
            record["relation_kinds"].update(relation_kinds)
            record["public_keywords"] = _merge_display_values(record["public_keywords"], keywords)
            record["references"].extend(_relation_references(raw, relation_kinds))
            if RISK_LEVELS[risk] > RISK_LEVELS[record["risk_level"]]:
                record["risk_level"] = risk
        if input_id:
            record["input_ids"].append(input_id)

    records = sorted(by_term.values(), key=lambda item: (item["term_key"], item["term"]))
    used_ids: set[str] = set()
    for record in records:
        input_ids = sorted({item for item in record["input_ids"] if _SAFE_ID.fullmatch(item)})
        candidate_id = input_ids[0] if input_ids else _safe_candidate_id("", record["identity_key"])
        if candidate_id in used_ids:
            candidate_id = _safe_candidate_id("", record["identity_key"] + record["term_key"])
        used_ids.add(candidate_id)
        record["candidate_id"] = candidate_id
        for input_id in record["input_ids"]:
            raw_id_to_term[normalize_text(input_id)] = record["term_key"]
    return records, raw_id_to_term, {
        "meeting_fact_verification_excluded": True,
        "network_admission_policy": "uncertain_only_v1",
        "allowed_verification_kinds": sorted(ALLOWED_VERIFICATION_KINDS),
        "excluded_verification_kinds": sorted(excluded_kinds),
        "dropped_candidate_count": dropped_candidate_count,
        "dropped_without_reason_candidate_count": dropped_without_reason_candidate_count,
        "admission_reason_counts": {
            reason: count for reason, count in admission_reason_counts.items() if count
        },
        "stripped_public_keyword_count": stripped_public_keyword_count,
        "stripped_relation_candidate_count": stripped_relation_candidate_count,
    }


def _merge_display_values(left: Iterable[str], right: Iterable[str]) -> list[str]:
    merged: dict[str, str] = {}
    for value in list(left) + list(right):
        shown = display_text(value)
        key = normalize_text(shown)
        if key and (key not in merged or (shown, key) < (merged[key], key)):
            merged[key] = shown
    return [merged[key] for key in sorted(merged)]


def _build_groups(records: list[dict[str, Any]], raw_id_to_term: dict[str, str]) -> list[dict[str, Any]]:
    index_by_term = {record["term_key"]: index for index, record in enumerate(records)}
    index_by_id = {
        normalize_text(record["candidate_id"]): index
        for index, record in enumerate(records)
    }
    term_or_alias: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        for key in {record["term_key"], *record["alias_keys"]}:
            term_or_alias.setdefault(key, []).append(index)

    union_find = _UnionFind(len(records))

    def union_values(values: dict[str, list[int]]) -> None:
        for indexes in values.values():
            indexes = sorted(set(indexes))
            for index in indexes[1:]:
                union_find.union(indexes[0], index)

    # A shared alias, relation identifier, or ambiguity set is an explicit
    # non-splittable relation.  Generic fuzzy similarity is deliberately not
    # used.
    union_values(term_or_alias)
    union_values(
        {
            key: [index for index, record in enumerate(records) if key in record["relation_id_keys"]]
            for key in sorted({key for record in records for key in record["relation_id_keys"]})
        }
    )
    union_values(
        {
            key: [index for index, record in enumerate(records) if key in record["ambiguity_keys"]]
            for key in sorted({key for record in records for key in record["ambiguity_keys"]})
        }
    )

    for index, record in enumerate(records):
        for reference in record["references"]:
            key = normalize_text(reference["reference"])
            target_term = raw_id_to_term.get(key, key)
            target_indexes = index_by_id.get(normalize_text(reference["reference"]))
            if target_indexes is None:
                target_indexes = index_by_term.get(target_term)
            if target_indexes is None:
                target_indexes_list = term_or_alias.get(target_term, [])
                if target_indexes_list:
                    target_indexes = target_indexes_list[0]
            if target_indexes is not None:
                union_find.union(index, target_indexes)

    members_by_root: dict[int, list[int]] = {}
    for index in range(len(records)):
        members_by_root.setdefault(union_find.find(index), []).append(index)

    groups: list[dict[str, Any]] = []
    for member_indexes in members_by_root.values():
        member_indexes.sort(key=lambda index: records[index]["candidate_id"])
        member_ids = [records[index]["candidate_id"] for index in member_indexes]
        group_id = f"group_{hashlib.sha256(canonical_sha256(member_ids).encode('ascii')).hexdigest()[:12]}"
        weight = 0
        high_risk_count = 0
        relation_kinds: set[str] = set()
        relation_ids: set[str] = set()
        ambiguity_sets: set[str] = set()
        for index in member_indexes:
            record = records[index]
            record["group_id"] = group_id
            candidate_weight = _candidate_weight(
                record["risk_level"], record["verification_kinds"], record["relation_kinds"]
            )
            record["risk_weight"] = candidate_weight
            weight += candidate_weight
            high_risk_count += int(record["risk_level"] == "high")
            relation_kinds.update(record["relation_kinds"])
            relation_ids.update(record["relation_ids"])
            ambiguity_sets.update(record["ambiguity_set"])
        groups.append(
            {
                "group_id": group_id,
                "candidate_ids": member_ids,
                "candidate_count": len(member_ids),
                "weight": weight,
                "high_risk_count": high_risk_count,
                "relation_kinds": sorted(relation_kinds),
                "relation_ids": sorted(relation_ids, key=normalize_text),
                "ambiguity_sets": sorted(ambiguity_sets, key=normalize_text),
            }
        )
    groups.sort(key=lambda group: group["group_id"])
    return groups


def _verification_packet(record: dict[str, Any]) -> dict[str, Any]:
    # Keep this exact allow-list small.  In particular, do not pass relation
    # context, source excerpts, local paths, or risk metadata to the network
    # verifier.
    return {
        "candidate_id": record["candidate_id"],
        "candidate_term": record["term"],
        "aliases": list(record["aliases"]),
        "verification_kinds": sorted(record["verification_kinds"]),
        "public_keywords": list(record["public_keywords"]),
    }


def _public_candidate(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": record["candidate_id"],
        "term": record["term"],
        "normalized_term": record["term_key"],
        "aliases": list(record["aliases"]),
        "verification_kinds": sorted(record["verification_kinds"]),
        "verification_reason_codes": sorted(record["verification_reason_codes"]),
        "public_keywords": list(record["public_keywords"]),
        "risk_level": record["risk_level"],
        "risk_weight": record["risk_weight"],
        "relation_kind": sorted(record["relation_kinds"]),
        "relation_ids": list(record["relation_ids"]),
        "ambiguity_set": list(record["ambiguity_set"]),
        "group_id": record["group_id"],
    }


def _build_shards(
    records: list[dict[str, Any]],
    groups: list[dict[str, Any]],
    *,
    shard_target_weight: int,
    max_shard_count: int,
) -> list[dict[str, Any]]:
    if not groups:
        return []
    total_weight = sum(int(group["weight"]) for group in groups)
    desired_shard_count = max(2, math.ceil(total_weight / shard_target_weight))
    shard_count = min(len(groups), max_shard_count, desired_shard_count)
    buckets = [
        {"group_ids": [], "candidate_ids": [], "weight": 0, "high_risk_count": 0}
        for _ in range(shard_count)
    ]
    group_by_id = {group["group_id"]: group for group in groups}
    record_by_id = {record["candidate_id"]: record for record in records}
    # Largest-first bin packing with stable group-id tie breaks.  The number
    # of bins depends only on candidate weights, not input order or task
    # completion order.
    for group in sorted(groups, key=lambda item: (-int(item["weight"]), item["group_id"])):
        bucket_index = min(
            range(shard_count),
            key=lambda index: (buckets[index]["weight"], len(buckets[index]["group_ids"]), index),
        )
        bucket = buckets[bucket_index]
        bucket["group_ids"].append(group["group_id"])
        bucket["candidate_ids"].extend(group["candidate_ids"])
        bucket["weight"] += int(group["weight"])
        bucket["high_risk_count"] += int(group["high_risk_count"])

    shards: list[dict[str, Any]] = []
    for number, bucket in enumerate(buckets, start=1):
        candidate_ids = sorted(
            bucket["candidate_ids"],
            key=lambda candidate_id: (int(record_by_id[candidate_id]["ordinal"]), candidate_id),
        )
        ordinals = [int(record_by_id[candidate_id]["ordinal"]) for candidate_id in candidate_ids]
        packet = [_verification_packet(record_by_id[candidate_id]) for candidate_id in candidate_ids]
        shard_id = f"shard_{number:03d}"
        artifact_type = f"entity_verification_shard__{shard_id}"
        shard_payload: dict[str, Any] = {
            "shard_id": shard_id,
            "artifact_type": artifact_type,
            "shard_number": number,
            "group_ids": sorted(bucket["group_ids"]),
            "candidate_ids": candidate_ids,
            "candidate_count": len(candidate_ids),
            "candidate_range": {
                "start": min(ordinals),
                "end": max(ordinals),
                "ordinals": ordinals,
            },
            "weight": int(bucket["weight"]),
            "high_risk_count": int(bucket["high_risk_count"]),
            "verification_packet": packet,
        }
        shard_digest = canonical_sha256(shard_payload)
        shard_payload["input_sha256"] = shard_digest
        shard_payload["shard_sha256"] = shard_digest
        shards.append(shard_payload)
    return shards


def build_manifest(
    payload: Any,
    *,
    mode: str = "auto",
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    shard_target_weight: int = DEFAULT_SHARD_TARGET_WEIGHT,
    max_entity_waves: int = DEFAULT_MAX_ENTITY_WAVES,
) -> dict[str, Any]:
    """Build an entity candidate manifest from a JSON object or candidate list."""

    if mode not in ALLOWED_MODES:
        raise ValueError("mode 必须是 auto、single 或 parallel")
    if not isinstance(max_parallel, int) or isinstance(max_parallel, bool) or not 1 <= max_parallel <= 8:
        raise ValueError("max_parallel 必须是 1 到 8 的整数")
    if (
        not isinstance(shard_target_weight, int)
        or isinstance(shard_target_weight, bool)
        or not 1 <= shard_target_weight <= MAX_SHARD_TARGET_WEIGHT
    ):
        raise ValueError(f"shard_target_weight 必须是 1 到 {MAX_SHARD_TARGET_WEIGHT} 的整数")
    if (
        not isinstance(max_entity_waves, int)
        or isinstance(max_entity_waves, bool)
        or not 1 <= max_entity_waves <= MAX_ENTITY_WAVES
    ):
        raise ValueError(f"max_entity_waves 必须是 1 到 {MAX_ENTITY_WAVES} 的整数")
    payload_object, raw_candidates = _input_candidates(payload)
    source_sha256, source_hash_kind = _source_hash(payload_object, raw_candidates)
    records, raw_id_to_term, scope_policy = _prepare_records(raw_candidates)
    if not records:
        raise ValueError("candidates 不得为空")
    groups = _build_groups(records, raw_id_to_term)
    for ordinal, record in enumerate(
        sorted(records, key=lambda item: (item["term_key"], item["term"], item["candidate_id"])),
        start=1,
    ):
        record["ordinal"] = ordinal
    records.sort(key=lambda item: int(item["ordinal"]))
    public_candidates = [_public_candidate(record) for record in records]
    candidate_set_sha256 = canonical_sha256(public_candidates)
    total_weight = sum(int(group["weight"]) for group in groups)
    high_risk_count = sum(int(record["risk_level"] == "high") for record in records)
    group_count = len(groups)
    if mode == "auto":
        selected_mode = (
            "single"
            if (
                len(records) <= DIRECT_CANDIDATE_LIMIT
                and total_weight <= DIRECT_WEIGHT_LIMIT
                and high_risk_count <= DIRECT_HIGH_RISK_LIMIT
            )
            or group_count <= 1
            else "parallel"
        )
    else:
        selected_mode = mode
    max_shard_count = max_parallel * max_entity_waves
    if selected_mode == "parallel" and (group_count < 2 or max_shard_count < 2):
        raise ValueError(
            "parallel 模式至少需要 2 个不可拆 group 和 2 个 shard 容量；"
            "请使用 single 或提高 max_parallel/max_entity_waves"
        )
    packet = [_verification_packet(record) for record in records]
    shards = (
        _build_shards(
            records,
            groups,
            shard_target_weight=shard_target_weight,
            max_shard_count=max_shard_count,
        )
        if selected_mode == "parallel"
        else []
    )
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifact_type": "entity_candidate_manifest",
        "artifact_owner": "Main Orchestrator",
        "source_sha256": source_sha256,
        "source_hash_kind": source_hash_kind,
        "candidate_set_sha256": candidate_set_sha256,
        "requested_mode": mode,
        "mode": selected_mode,
        "policy": {
            "max_parallel": max_parallel,
            "shard_target_weight": shard_target_weight,
            "max_entity_waves": max_entity_waves,
            "max_shard_count": max_shard_count,
            "direct_candidate_limit": DIRECT_CANDIDATE_LIMIT,
            "direct_weight_limit": DIRECT_WEIGHT_LIMIT,
            "direct_high_risk_limit": DIRECT_HIGH_RISK_LIMIT,
        },
        "scope_policy": scope_policy,
        "candidate_count": len(public_candidates),
        "group_count": len(groups),
        "shard_count": len(shards),
        "total_weight": total_weight,
        "high_risk_count": high_risk_count,
        "candidates": public_candidates,
        "groups": groups,
        "single_packet": packet if selected_mode == "single" else [],
        "shards": shards,
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    return manifest


def build_entity_candidate_manifest(
    candidates: list[dict[str, Any]],
    *,
    source_sha256: str = "",
    mode: str = "auto",
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    shard_target_weight: int = DEFAULT_SHARD_TARGET_WEIGHT,
    max_entity_waves: int = DEFAULT_MAX_ENTITY_WAVES,
) -> dict[str, Any]:
    """Convenience API for callers that already have a candidate list."""

    payload: dict[str, Any] = {"candidates": candidates}
    if source_sha256:
        payload["source_sha256"] = source_sha256
    return build_manifest(
        payload,
        mode=mode,
        max_parallel=max_parallel,
        shard_target_weight=shard_target_weight,
        max_entity_waves=max_entity_waves,
    )


# Short import-friendly alias used by lightweight regression helpers.
build_candidate_manifest = build_manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成可控并行实体核验的候选 manifest")
    parser.add_argument("input", type=Path, help="UTF-8 候选 JSON 路径")
    parser.add_argument("--out", type=Path, required=True, help="manifest JSON 输出路径")
    parser.add_argument("--mode", choices=sorted(ALLOWED_MODES), default="auto")
    parser.add_argument("--max-parallel", type=int, default=DEFAULT_MAX_PARALLEL)
    parser.add_argument("--shard-target-weight", type=int, default=DEFAULT_SHARD_TARGET_WEIGHT)
    parser.add_argument("--max-entity-waves", type=int, default=DEFAULT_MAX_ENTITY_WAVES)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        raw_bytes = args.input.read_bytes()
        if raw_bytes.startswith(b"\xef\xbb\xbf"):
            raise ValueError("输入文件必须为 UTF-8 without BOM")
        payload = json.loads(raw_bytes.decode("utf-8"))
        manifest = build_manifest(
            payload,
            mode=args.mode,
            max_parallel=args.max_parallel,
            shard_target_weight=args.shard_target_weight,
            max_entity_waves=args.max_entity_waves,
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "out": str(args.out),
                "mode": manifest["mode"],
                "candidate_count": manifest["candidate_count"],
                "group_count": manifest["group_count"],
                "shard_count": manifest["shard_count"],
                "manifest_sha256": manifest["manifest_sha256"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
