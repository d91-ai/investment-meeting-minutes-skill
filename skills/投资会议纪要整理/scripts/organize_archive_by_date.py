#!/usr/bin/env python3
"""Organize Obsidian archive files into date folders."""

from __future__ import annotations

import argparse
import os
import re
import shutil
from pathlib import Path

DEFAULT_WORKSPACE_ROOT = (
    Path(os.environ["INVESTMENT_MINUTES_WORKSPACE"]).expanduser()
    if os.environ.get("INVESTMENT_MINUTES_WORKSPACE")
    else Path.home() / "Documents/会议纪要整理"
)
DEFAULT_ARCHIVE_DIR = DEFAULT_WORKSPACE_ROOT / "04 Archive/测试用"


def detect_date(name: str) -> str:
    compact = re.search(r"(20\d{2})(\d{2})(\d{2})", name)
    if compact:
        year, month, day = compact.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    loose = re.search(r"(20\d{2})[-_年. ]+(\d{1,2})[-_月. ]+(\d{1,2})", name)
    if loose:
        year, month, day = loose.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    if name.startswith("a73920c52c89f3c15a240b03c51bcf07"):
        return "2026-04-20"

    return "未分类"


def unique_target(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成不冲突文件名: {path}")


def organize_archive(archive_dir: Path) -> list[tuple[Path, Path]]:
    archive_dir.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    for source in sorted(archive_dir.iterdir()):
        if not source.is_file() or source.name == ".DS_Store":
            continue
        date_folder = archive_dir / detect_date(source.name)
        date_folder.mkdir(exist_ok=True)
        target = unique_target(date_folder / source.name)
        shutil.move(str(source), str(target))
        moved.append((source, target))
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description="按日期整理 Obsidian 归档目录")
    parser.add_argument("--archive-dir", default=str(DEFAULT_ARCHIVE_DIR))
    args = parser.parse_args()

    try:
        moved = organize_archive(Path(args.archive_dir).expanduser().resolve())
    except PermissionError as exc:
        print(f"Archive organize skipped: permission denied ({exc.filename})")
        return 0
    print(f"Archive organized: {len(moved)} files moved")
    for source, target in moved:
        print(f"{source.name} -> {target.parent.name}/{target.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
