#!/usr/bin/env python3
"""Coordinate readers and writers for one MAS dispatch directory."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from portable_flock import LOCK_EX, LOCK_SH, LOCK_UN, flock


@contextmanager
def mas_task_lock(task_dir: Path, *, exclusive: bool) -> Iterator[None]:
    task_dir.mkdir(parents=True, exist_ok=True)
    lock_path = task_dir / ".mas-task.lock"
    operation = LOCK_EX if exclusive else LOCK_SH
    with lock_path.open("a+b") as handle:
        flock(handle.fileno(), operation)
        try:
            yield
        finally:
            flock(handle.fileno(), LOCK_UN)
