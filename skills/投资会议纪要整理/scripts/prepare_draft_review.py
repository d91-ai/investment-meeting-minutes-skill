#!/usr/bin/env python3
"""Freeze a structurally valid Markdown draft before semantic review dispatch."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from validate_meeting_minutes_contract import (
    SOURCE_MODE_CHOICES,
    content_bold_terms,
    inline_doubtful_mapping_findings,
    validate_contract,
    validate_verification_sidecar,
)
from validate_utf8_text import validate_file


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def default_verification_path(markdown_path: Path) -> Path | None:
    for suffix in (".verification.json", ".verification.jsonl"):
        candidate = markdown_path.with_name(markdown_path.stem + suffix)
        if candidate.is_file():
            return candidate
    return None


def prepare_draft_review(
    markdown_path: Path,
    *,
    verification_path: Path | None = None,
    source_mode: str = "auto",
) -> dict[str, Any]:
    markdown_path = markdown_path.expanduser().resolve()
    errors: list[str] = []
    warnings: list[str] = []

    try:
        utf8_ok, utf8_message = validate_file(markdown_path, require_cjk=True)
    except Exception as exc:
        utf8_ok, utf8_message = False, str(exc)
    if not utf8_ok:
        errors.append(utf8_message)

    markdown = ""
    if utf8_ok:
        try:
            markdown = markdown_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"Markdown cannot be read as UTF-8: {exc}")

    if markdown:
        contract = validate_contract(markdown, source_mode=source_mode)
        errors.extend(str(item) for item in contract.get("errors", []))
        warnings.extend(str(item) for item in contract.get("warnings", []))

        verification_path = verification_path or default_verification_path(markdown_path)
        has_inline_doubtful = bool(content_bold_terms(markdown))
        if verification_path is not None or has_inline_doubtful:
            verification = validate_verification_sidecar(
                verification_path,
                require_verification=has_inline_doubtful,
            )
            errors.extend(str(item) for item in verification.get("errors", []))
            warnings.extend(str(item) for item in verification.get("warnings", []))
            if verification.get("ok"):
                errors.extend(inline_doubtful_mapping_findings(markdown, verification_path))

    ready = not errors
    return {
        "schema_version": "1.0",
        "ready_for_semantic_review": ready,
        "markdown_path": str(markdown_path),
        "markdown_sha256": file_sha256(markdown_path) if ready else "",
        "verification_path": str(verification_path.resolve()) if verification_path else "",
        "source_mode": source_mode,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run deterministic gates and freeze exact Markdown bytes before semantic review"
    )
    parser.add_argument("markdown_file", help="Main-workflow-owned Markdown draft")
    parser.add_argument("--verification", help="Optional verification JSON/JSONL sidecar")
    parser.add_argument(
        "--source-mode",
        choices=SOURCE_MODE_CHOICES,
        default="auto",
    )
    parser.add_argument("--out", help="Optional JSON receipt path")
    parser.add_argument("--json", action="store_true", help="Print JSON; default is also JSON")
    args = parser.parse_args()

    result = prepare_draft_review(
        Path(args.markdown_file),
        verification_path=Path(args.verification).expanduser() if args.verification else None,
        source_mode=args.source_mode,
    )
    if args.out:
        output_path = Path(args.out).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready_for_semantic_review"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
