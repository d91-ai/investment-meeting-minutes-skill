#!/usr/bin/env python3
"""Build a small external-search plan for one uncertain non-person entity."""

from __future__ import annotations

import argparse
import json
import re
import sys


PINYIN_PATTERN = re.compile(r"^[a-z]+(?: [a-z]+)*$")


def normalize_pinyin(value: str) -> list[str]:
    normalized = value.lower().replace("'", " ").replace("-", " ")
    normalized = re.sub(r"[1-5]", "", normalized)
    normalized = " ".join(normalized.split())
    if not PINYIN_PATTERN.fullmatch(normalized):
        raise ValueError("--pinyin 必须是以空格分隔的 ASCII 拼音音节")
    return normalized.split()


def build_queries(
    observed: str,
    syllables: list[str],
    candidates: list[str],
    contexts: list[str],
) -> list[dict[str, str]]:
    suffix = " ".join(contexts)
    terms = [observed, *candidates]
    unique_terms = list(dict.fromkeys(term for term in terms if term))
    alternatives = [*(f'"{term}"' for term in unique_terms), f'"{" ".join(syllables)}"', "".join(syllables)]
    query = f"({' OR '.join(dict.fromkeys(alternatives))}) {suffix}".strip()
    return [{"tier": "combined", "query": query}]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observed", help="来源中的疑似专名")
    parser.add_argument("--pinyin", required=True, help="基模给出的完整读音，例如 dai le xin cai")
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        help="基模依音形提出的一至三个候选写法；可重复",
    )
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        help="一至三个公开、非关系型限定词，例如 A股 或 公司",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    try:
        observed = args.observed.strip()
        candidates = list(dict.fromkeys(item.strip() for item in args.candidate if item.strip()))
        contexts = list(dict.fromkeys(item.strip() for item in args.context if item.strip()))
        if not observed:
            raise ValueError("observed 不能为空")
        if len(candidates) > 3:
            raise ValueError("最多提供三个 --candidate")
        if any(len(item) > 40 or "\n" in item or "\r" in item for item in candidates):
            raise ValueError("--candidate 必须是简短的名称候选")
        if not 1 <= len(contexts) <= 3:
            raise ValueError("请提供一至三个 --context")
        if any(len(item) > 40 or "\n" in item or "\r" in item for item in contexts):
            raise ValueError("--context 必须是简短的公开限定词")
        syllables = normalize_pinyin(args.pinyin)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = {
        "observed": observed,
        "suggested_candidates": candidates,
        "candidate_only": True,
        "queries": build_queries(observed, syllables, candidates, contexts),
        "guidance": "每个实体只生成一个包含原词、少量音形候选与完整读音的组合查询；将所有实体合并为本场至多一轮外部请求。结果只扩充候选，不能单独确认正文，不追查。",
    }
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for row in payload["queries"]:
            print(f"{row['tier']}\t{row['query']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
