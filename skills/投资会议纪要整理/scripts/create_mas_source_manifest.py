#!/usr/bin/env python3
"""Create the main-owned MAS source_manifest artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from validate_mas_artifacts import validate_payload

AUDIO_EXTENSIONS = {".aac", ".aiff", ".flac", ".m4a", ".mp3", ".mp4", ".wav"}
DOCUMENT_EXTENSIONS = {".doc", ".docx", ".md", ".srt", ".txt", ".vtt"}
PDF_EXTENSIONS = {".pdf"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any, overwrite: bool = False) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing source_manifest artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def infer_material_kind(name: str) -> str:
    lowered = name.lower()
    suffix = Path(lowered).suffix
    if "timestamp" in lowered or "time_index" in lowered:
        return "timestamp_index"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in PDF_EXTENSIONS:
        return "pdf_attachment"
    if suffix in DOCUMENT_EXTENSIONS:
        return "document"
    if suffix == ".json":
        return "metadata"
    return "material"


def safe_material_name(value: str) -> str:
    value = value.strip()
    if not value:
        return "unnamed_material"
    return Path(value).name if "/" in value else value


def normalize_material(item: Any) -> dict[str, str]:
    if isinstance(item, dict):
        raw_name = str(item.get("name") or item.get("file") or item.get("path") or "unnamed_material")
        name = safe_material_name(raw_name)
        kind = str(item.get("kind") or infer_material_kind(name))
        material = {"kind": kind, "name": name}
        status = item.get("status")
        if status not in (None, ""):
            material["status"] = str(status)
        elif kind == "pdf_attachment":
            material["status"] = "requires_extracted_text"
        return material
    name = safe_material_name(str(item))
    kind = infer_material_kind(name)
    material = {"kind": kind, "name": name}
    if kind == "pdf_attachment":
        material["status"] = "requires_extracted_text"
    return material


def load_context(request_json: Path | None, bundle_json: Path | None, task_dir: Path | None) -> dict[str, Any]:
    if request_json:
        payload = read_json(request_json)
    elif bundle_json:
        payload = read_json(bundle_json)
    elif task_dir and (task_dir / "mas_task_bundle.json").exists():
        payload = read_json(task_dir / "mas_task_bundle.json")
    else:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("source manifest context must be a JSON object")
    if task_dir and (task_dir / "mas_task_bundle.json").exists() and not payload.get("run_id"):
        bundle_payload = read_json(task_dir / "mas_task_bundle.json")
        if isinstance(bundle_payload, dict) and bundle_payload.get("run_id"):
            payload = {**payload, "run_id": bundle_payload["run_id"]}
    return payload


def create_source_manifest(
    context: dict[str, Any],
    archive_allowed: bool = False,
    archive_status: str = "not_started",
    skipped_reason: str = "",
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    materials = [normalize_material(item) for item in context.get("materials", [])]
    if not materials:
        warnings.append("source_manifest materials is empty")
    if not skipped_reason and not archive_allowed:
        skipped_reason = "archive_not_confirmed_by_main_workflow"
    manifest = {
        "source_mode": str(context.get("source_mode") or "document_only"),
        "materials": materials,
        "archive_allowed": bool(archive_allowed),
        "archive_status": archive_status,
        "skipped_reason": skipped_reason,
    }
    return manifest, warnings


def source_manifest_artifact(manifest: dict[str, Any], run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "task_id": f"{run_id}:main:source_manifest",
        "dispatch_phase": "pre_draft",
        "artifact_owner": "Main Orchestrator",
        "artifact_type": "source_manifest",
        "artifact": manifest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a main-owned MAS source_manifest artifact")
    parser.add_argument("--request-json", help="MAS request JSON")
    parser.add_argument("--bundle-json", help="MAS task bundle JSON")
    parser.add_argument("--task-dir", help="MAS dispatch directory; defaults output to artifacts/source_manifest.json")
    parser.add_argument("--out", help="Artifact JSON output path")
    parser.add_argument("--archive-allowed", action="store_true", help="Set source_manifest.archive_allowed=true")
    parser.add_argument("--archive-status", default="not_started", help="source_manifest.archive_status")
    parser.add_argument("--skipped-reason", default="", help="source_manifest.skipped_reason")
    parser.add_argument("--overwrite", action="store_true", help="Allow overwriting an existing source_manifest artifact")
    parser.add_argument("--json", action="store_true", help="Print JSON; default is also JSON")
    args = parser.parse_args()

    task_dir = Path(args.task_dir).expanduser() if args.task_dir else None
    errors: list[str] = []
    warnings: list[str] = []
    artifact_file = ""
    artifact: dict[str, Any] | None = None
    try:
        context = load_context(
            Path(args.request_json).expanduser() if args.request_json else None,
            Path(args.bundle_json).expanduser() if args.bundle_json else None,
            task_dir,
        )
        manifest, manifest_warnings = create_source_manifest(
            context,
            archive_allowed=bool(args.archive_allowed),
            archive_status=str(args.archive_status),
            skipped_reason=str(args.skipped_reason),
        )
        warnings.extend(manifest_warnings)
        run_id = str(context.get("run_id") or "")
        if not run_id:
            raise ValueError("source_manifest context missing dispatch run_id")
        artifact = source_manifest_artifact(manifest, run_id)
        validation = validate_payload(artifact, required_artifacts=["source_manifest"])
        errors.extend(str(error) for error in validation.get("errors", []))
        if not errors:
            out_path = Path(args.out).expanduser() if args.out else None
            if out_path is None and task_dir is not None:
                out_path = task_dir / "artifacts" / "source_manifest.json"
            if out_path is not None:
                write_json(out_path, artifact, overwrite=bool(args.overwrite))
                artifact_file = str(out_path)
    except Exception as exc:
        errors.append(f"source_manifest creation failed: {exc.__class__.__name__}: {exc}")
        validation = {"ok": False, "errors": errors, "warnings": warnings}

    result = {
        "schema_version": "1.0",
        "ok": not errors,
        "artifact_type": "source_manifest",
        "artifact_file": artifact_file,
        "artifact": artifact.get("artifact") if artifact else {},
        "validation": validation,
        "errors": errors,
        "warnings": warnings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
