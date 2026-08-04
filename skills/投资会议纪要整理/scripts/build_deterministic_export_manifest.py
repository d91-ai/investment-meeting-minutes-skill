#!/usr/bin/env python3
"""Build a main-owned, deterministic MAS export manifest.

The final Markdown is an input to this command and is never written by it.  The
command binds the Markdown, the main-action receipt, the current task bundle,
and explicit validator/regression evidence by SHA-256.  A specialist cannot
make an export pass merely by returning a self-reported ``export_manifest``.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from mas_task_lock import mas_task_lock
from validate_mas_artifacts import HEX_SHA256, canonical_json_digest, file_sha256, read_json


SCHEMA_VERSION = "2.0"
MANIFEST_MODE = "deterministic_main_owned_v1"
ARTIFACT_TYPE = "export_manifest"
ARTIFACT_OWNER = "Main Orchestrator"
DISPATCH_PHASE = "final_verification"
REQUIRED_VALIDATOR_NAMES = {
    "validate_utf8_text.py",
    "validate_meeting_minutes_contract.py",
}
REGRESSION_VALIDATOR_NAME = "run_meeting_minutes_regression.py"
EVIDENCE_NAME_KEYS = ("name", "validator_name", "check_name")


class ExportManifestError(ValueError):
    """Raised when an input cannot be safely bound to the current run."""


def _nonempty(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ExportManifestError(f"{label} must be a non-empty string")
    return text


def _sha256(value: Any, label: str) -> str:
    text = str(value or "").strip().lower()
    if not HEX_SHA256.fullmatch(text):
        raise ExportManifestError(f"{label} must be a 64-character lowercase SHA-256")
    return text


def _resolve_existing(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ExportManifestError(f"{label} does not exist or is not a file: {resolved}")
    return resolved


def _resolve_optional(path: Path | None, label: str) -> Path | None:
    if path is None:
        return None
    return _resolve_existing(path, label)


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except Exception as exc:  # pragma: no cover - exact JSON parser varies by Python
        raise ExportManifestError(f"{label} is not readable UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExportManifestError(f"{label} must be a JSON object")
    return payload


def _unwrap_artifact(payload: dict[str, Any], expected_type: str, label: str) -> dict[str, Any]:
    """Accept a normal MAS envelope, artifacts map, or direct artifact object."""

    artifact_type = str(payload.get("artifact_type") or "").strip()
    if artifact_type:
        if artifact_type != expected_type:
            raise ExportManifestError(
                f"{label} artifact_type must be {expected_type}, got {artifact_type}"
            )
        artifact = payload.get("artifact")
        if not isinstance(artifact, dict):
            raise ExportManifestError(f"{label}.artifact must be a JSON object")
        return artifact
    artifacts = payload.get("artifacts")
    if isinstance(artifacts, dict) and expected_type in artifacts:
        artifact = artifacts[expected_type]
        if not isinstance(artifact, dict):
            raise ExportManifestError(f"{label}.artifacts.{expected_type} must be a JSON object")
        return artifact
    # A receipt passed directly is useful for local repair commands, while the
    # output still records its exact source file hash.
    if expected_type == "main_action_receipt" and "status" in payload:
        return payload
    raise ExportManifestError(
        f"{label} must be a {expected_type} MAS envelope or direct artifact object"
    )


def _extract_evidence_object(payload: dict[str, Any], label: str) -> dict[str, Any]:
    """Extract one explicit validator result without inferring pass/fail."""

    candidate: Any = payload
    for key in ("result", "validation", "regression_result", "validator_result"):
        nested = payload.get(key)
        if isinstance(nested, dict) and not any(key_name in payload for key_name in EVIDENCE_NAME_KEYS):
            candidate = nested
            break
    if not isinstance(candidate, dict):
        raise ExportManifestError(f"{label} evidence must be a JSON object")
    name = ""
    for key in EVIDENCE_NAME_KEYS:
        if str(candidate.get(key) or "").strip():
            name = str(candidate[key]).strip()
            break
    if not name:
        raise ExportManifestError(f"{label} evidence must explicitly provide name")
    if not isinstance(candidate.get("ok"), bool):
        raise ExportManifestError(f"{label} evidence.ok must be boolean")
    return candidate


def _read_evidence(path: Path, label: str, expected_name: str) -> dict[str, Any]:
    payload = _read_object(path, label)
    evidence = _extract_evidence_object(payload, label)
    actual_name = str(evidence.get("name") or evidence.get("validator_name") or evidence.get("check_name") or "").strip()
    if actual_name != expected_name:
        raise ExportManifestError(
            f"{label} evidence name must be {expected_name}, got {actual_name or '<empty>'}"
        )
    return evidence


def _validator_evidence(path: Path) -> dict[str, Any]:
    label = f"validator evidence {path}"
    payload = _read_object(path, label)
    evidence = _extract_evidence_object(payload, label)
    name = str(evidence.get("name") or evidence.get("validator_name") or evidence.get("check_name") or "").strip()
    if name not in REQUIRED_VALIDATOR_NAMES:
        raise ExportManifestError(
            f"{label} contains unknown validator name: {name or '<empty>'}"
        )
    return evidence


def _regression_evidence(path: Path) -> dict[str, Any]:
    label = f"regression evidence {path}"
    payload = _read_object(path, label)
    evidence = _extract_evidence_object(payload, label)
    name = str(evidence.get("name") or evidence.get("validator_name") or evidence.get("check_name") or "").strip()
    if name != REGRESSION_VALIDATOR_NAME:
        raise ExportManifestError(
            f"{label} evidence name must be {REGRESSION_VALIDATOR_NAME}, got {name or '<empty>'}"
        )
    case_count = evidence.get("case_count")
    if not isinstance(case_count, int) or isinstance(case_count, bool) or case_count <= 0:
        raise ExportManifestError(f"{label} case_count must be a positive integer")
    return evidence


def _receipt_artifact(path: Path, run_id: str, markdown_path: Path, task_dir: Path) -> dict[str, Any]:
    payload = _read_object(path, "main_action_receipt")
    if str(payload.get("run_id") or "").strip() and str(payload.get("run_id")) != run_id:
        raise ExportManifestError("main_action_receipt envelope run_id does not match task bundle")
    if str(payload.get("artifact_type") or "").strip():
        if str(payload.get("artifact_owner") or "").strip() != ARTIFACT_OWNER:
            raise ExportManifestError("main_action_receipt artifact_owner must be Main Orchestrator")
        if str(payload.get("task_id") or "").strip() != f"{run_id}:main:main_action_receipt":
            raise ExportManifestError("main_action_receipt task_id does not match task bundle")
        if str(payload.get("dispatch_phase") or "").strip() != "draft_review":
            raise ExportManifestError("main_action_receipt dispatch_phase must be draft_review")
    receipt = _unwrap_artifact(payload, "main_action_receipt", "main_action_receipt")
    if str(receipt.get("run_id") or "").strip() != run_id:
        raise ExportManifestError("main_action_receipt.run_id does not match task bundle")
    if receipt.get("status") != "applied":
        raise ExportManifestError("main_action_receipt.status must be applied")
    receipt_markdown_value = Path(str(receipt.get("markdown_path") or ""))
    if not receipt_markdown_value.is_absolute():
        receipt_markdown_value = task_dir / receipt_markdown_value
    receipt_markdown = _resolve_existing(receipt_markdown_value, "main_action_receipt Markdown")
    if receipt_markdown != markdown_path:
        raise ExportManifestError(
            "main_action_receipt.markdown_path does not match the requested final Markdown"
        )
    recorded_hash = _sha256(receipt.get("markdown_sha256"), "main_action_receipt.markdown_sha256")
    actual_hash = file_sha256(markdown_path)
    if recorded_hash != actual_hash:
        raise ExportManifestError("main_action_receipt.markdown_sha256 is stale")
    _sha256(receipt.get("source_artifact_digest"), "main_action_receipt.source_artifact_digest")
    actions = receipt.get("actions")
    if not isinstance(actions, list) or not actions or any(not str(item).strip() for item in actions):
        raise ExportManifestError("main_action_receipt.actions must be a non-empty string array")
    return receipt


def _sidecar_details(path: Path | None) -> tuple[str, str, list[str]]:
    if path is None:
        return "", "", []
    payload = _read_object(path, "verification sidecar")
    records = payload.get("records")
    if not isinstance(records, list):
        raise ExportManifestError("verification sidecar.records must be a JSON array")
    known: list[str] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ExportManifestError(f"verification sidecar.records[{index}] must be a JSON object")
        needs_sidecar = record.get("是否需要 sidecar")
        if needs_sidecar is not None and not isinstance(needs_sidecar, bool):
            raise ExportManifestError(
                f"verification sidecar.records[{index}].是否需要 sidecar must be boolean"
            )
        if needs_sidecar is True:
            original = str(record.get("原始表述") or "").strip()
            if not original:
                raise ExportManifestError(
                    f"verification sidecar.records[{index}] needing sidecar must have 原始表述"
                )
            known.append(original)
    return str(path), file_sha256(path), sorted(set(known))


def _bundle_binding(task_dir: Path) -> tuple[dict[str, Any], Path, str, str]:
    bundle_path = _resolve_existing(task_dir / "mas_task_bundle.json", "MAS task bundle")
    bundle = _read_object(bundle_path, "MAS task bundle")
    run_id = _nonempty(bundle.get("run_id"), "MAS task bundle.run_id")
    return bundle, bundle_path, file_sha256(bundle_path), run_id


def _run_binding(task_dir: Path, bundle: dict[str, Any], run_id: str) -> dict[str, str]:
    """Bind an optional current run summary when one is available.

    A bundle/run_id is always required.  If the operator wrote a run summary,
    its bytes are also bound so a later collector cannot silently mix runs.
    """

    binding: dict[str, str] = {"run_id": run_id}
    summary_path = task_dir / "mas_run_summary.json"
    if summary_path.is_file():
        summary = _read_object(summary_path, "MAS run summary")
        summary_run_id = str(summary.get("run_id") or bundle.get("run_id") or "").strip()
        if summary_run_id and summary_run_id != run_id:
            raise ExportManifestError("MAS run summary.run_id does not match task bundle")
        binding.update(
            {
                "run_summary_path": str(summary_path.resolve()),
                "run_summary_sha256": file_sha256(summary_path),
            }
        )
    return binding


def _evidence_record(path: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    name = str(evidence.get("name") or evidence.get("validator_name") or evidence.get("check_name") or "").strip()
    return {
        "name": name,
        "ok": evidence["ok"],
        **({"case_count": evidence["case_count"]} if "case_count" in evidence else {}),
        "evidence_path": str(path.resolve()),
        "evidence_sha256": file_sha256(path),
    }


def build_deterministic_export_manifest(
    task_dir: Path,
    markdown_path: Path,
    *,
    verification_sidecar_path: Path | None = None,
    main_action_receipt_path: Path | None = None,
    validator_evidence_paths: Iterable[Path] = (),
    regression_evidence_path: Path | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Validate current-run inputs and atomically write one main-owned manifest."""

    task_dir = task_dir.expanduser().resolve()
    markdown_path = _resolve_existing(markdown_path, "final Markdown")
    sidecar_path = _resolve_optional(verification_sidecar_path, "verification sidecar")
    receipt_path = _resolve_existing(
        main_action_receipt_path or task_dir / "artifacts" / "main_action_receipt.json",
        "main_action_receipt",
    )
    regression_path = _resolve_existing(regression_evidence_path, "regression evidence") if regression_evidence_path else None
    if regression_path is None:
        raise ExportManifestError("regression_evidence_path is required")

    validator_paths = [_resolve_existing(path, "validator evidence") for path in validator_evidence_paths]
    if len(validator_paths) != len(set(validator_paths)):
        raise ExportManifestError("validator evidence paths must be unique")
    if regression_path in validator_paths:
        raise ExportManifestError("regression evidence must not reuse a validator evidence file")

    with mas_task_lock(task_dir, exclusive=True):
        bundle, bundle_path, bundle_sha256, run_id = _bundle_binding(task_dir)
        run_binding = _run_binding(task_dir, bundle, run_id)
        receipt = _receipt_artifact(receipt_path, run_id, markdown_path, task_dir)
        if not validator_paths:
            raise ExportManifestError("at least one validator evidence JSON path is required")
        validator_evidence = [_validator_evidence(path) for path in validator_paths]
        validator_records = [_evidence_record(path, evidence) for path, evidence in zip(validator_paths, validator_evidence)]
        validator_records.sort(key=lambda item: (str(item["name"]), str(item["evidence_path"])))
        validator_names = {str(item["name"]) for item in validator_records}
        missing = REQUIRED_VALIDATOR_NAMES - validator_names
        if missing:
            raise ExportManifestError("validator evidence missing required names: " + ", ".join(sorted(missing)))
        if len(validator_names) != len(validator_records):
            raise ExportManifestError("validator evidence contains duplicate validator names")
        regression_evidence = _regression_evidence(regression_path)
        regression_record = _evidence_record(regression_path, regression_evidence)
        if any(not item["ok"] for item in validator_records) or not regression_record["ok"]:
            raise ExportManifestError("validator/regression evidence contains ok=false; export remains blocked")

        sidecar_value, sidecar_sha256, known_unverified = _sidecar_details(sidecar_path)
        markdown_sha256 = file_sha256(markdown_path)
        receipt_sha256 = file_sha256(receipt_path)
        artifact: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "manifest_mode": MANIFEST_MODE,
            "generation_mode": MANIFEST_MODE,
            "run_id": run_id,
            "markdown_path": str(markdown_path),
            "markdown_sha256": markdown_sha256,
            "verification_sidecar_path": sidecar_value,
            "verification_sidecar_sha256": sidecar_sha256,
            "main_action_receipt_sha256": receipt_sha256,
            "validator_evidence_sha256": canonical_json_digest(validator_records),
            "regression_evidence_sha256": str(regression_record["evidence_sha256"]),
            "validators_run": validator_records,
            "regression_result": regression_record,
            "export_status": "passed",
            "known_unverified_parts": known_unverified,
            "main_actions_verified": True,
            "bindings": {
                "run": run_binding,
                "task_bundle": {
                    "path": str(bundle_path),
                    "sha256": bundle_sha256,
                    "run_id": run_id,
                },
                "main_action_receipt": {
                    "path": str(receipt_path),
                    "sha256": receipt_sha256,
                    "run_id": str(receipt.get("run_id") or ""),
                    "markdown_sha256": markdown_sha256,
                },
                "verification_sidecar": {
                    "path": sidecar_value,
                    "sha256": sidecar_sha256,
                },
                "validator_evidence": validator_records,
                "regression_evidence": regression_record,
            },
        }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "manifest_mode": MANIFEST_MODE,
            "run_id": run_id,
            "task_id": f"{run_id}:main:{ARTIFACT_TYPE}",
            "dispatch_phase": DISPATCH_PHASE,
            "artifact_owner": ARTIFACT_OWNER,
            "artifact_type": ARTIFACT_TYPE,
            "artifact": artifact,
        }
        output_path = task_dir / "artifacts" / "export_manifest.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        archived_path = ""
        if output_path.exists():
            if not replace:
                raise FileExistsError(f"export_manifest already exists; pass --replace: {output_path}")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            archive_path = task_dir / "repair_history" / (
                f"{stamp}-{uuid.uuid4().hex[:12]}-export_manifest-superseded.json"
            )
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path.write_bytes(output_path.read_bytes())
            archived_path = str(archive_path)
        raw = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".export_manifest.", suffix=".tmp", dir=output_path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, output_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return {
            "schema_version": SCHEMA_VERSION,
            "manifest_mode": MANIFEST_MODE,
            "ok": True,
            "task_dir": str(task_dir),
            "run_id": run_id,
            "artifact_file": str(output_path),
            "archived_artifact_file": archived_path,
            "markdown_sha256": markdown_sha256,
            "validator_count": len(validator_records),
            "regression_case_count": regression_record["case_count"],
            "errors": [],
        }


# Short alias for callers that use the artifact's conventional name.
build_export_manifest = build_deterministic_export_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a deterministic main-owned MAS export_manifest")
    parser.add_argument("--task-dir", required=True, help="MAS dispatch directory")
    parser.add_argument("--markdown-path", required=True, help="Current final Markdown; never modified")
    parser.add_argument(
        "--verification-sidecar", "--sidecar-path", dest="verification_sidecar", help="Optional verification sidecar JSON"
    )
    parser.add_argument(
        "--main-action-receipt", "--main-action-receipt-path", dest="main_action_receipt",
        help="Main-action receipt JSON; defaults to task-dir/artifacts/main_action_receipt.json",
    )
    parser.add_argument(
        "--validator-evidence", "--validator-json", "--validator-evidence-json",
        dest="validator_evidence", action="append", default=[],
        help="Validator evidence JSON path; repeat once per known validator",
    )
    parser.add_argument(
        "--regression-evidence", "--regression-json", "--regression-evidence-json",
        dest="regression_evidence", required=True,
        help="run_meeting_minutes_regression.py JSON evidence path",
    )
    parser.add_argument("--replace", action="store_true", help="Archive and replace an existing manifest")
    parser.add_argument("--json", action="store_true", help="Print a JSON result (default output is also JSON)")
    args = parser.parse_args()
    try:
        result = build_deterministic_export_manifest(
            Path(args.task_dir),
            Path(args.markdown_path),
            verification_sidecar_path=Path(args.verification_sidecar) if args.verification_sidecar else None,
            main_action_receipt_path=Path(args.main_action_receipt) if args.main_action_receipt else None,
            validator_evidence_paths=[Path(item) for item in args.validator_evidence],
            regression_evidence_path=Path(args.regression_evidence),
            replace=bool(args.replace),
        )
    except Exception as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "manifest_mode": MANIFEST_MODE,
            "ok": False,
            "errors": [f"build deterministic export_manifest failed: {exc.__class__.__name__}: {exc}"],
        }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
