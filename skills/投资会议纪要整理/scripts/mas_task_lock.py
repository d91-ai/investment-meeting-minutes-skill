#!/usr/bin/env python3
"""Coordinate readers and writers for one MAS dispatch directory."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Iterator

if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", wintypes.WPARAM),
            ("InternalHigh", wintypes.WPARAM),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _kernel32.LockFileEx.restype = wintypes.BOOL
    _kernel32.UnlockFileEx.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _kernel32.UnlockFileEx.restype = wintypes.BOOL
else:
    import fcntl


def _lock(handle: object, *, exclusive: bool) -> None:
    if os.name == "nt":
        flags = 0x00000002 if exclusive else 0
        overlapped = _Overlapped()
        os_handle = msvcrt.get_osfhandle(handle.fileno())
        if not _kernel32.LockFileEx(os_handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    fcntl.flock(handle.fileno(), operation)


def _unlock(handle: object) -> None:
    if os.name == "nt":
        overlapped = _Overlapped()
        os_handle = msvcrt.get_osfhandle(handle.fileno())
        if not _kernel32.UnlockFileEx(os_handle, 0, 1, 0, ctypes.byref(overlapped)):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def mas_task_lock(task_dir: Path, *, exclusive: bool) -> Iterator[None]:
    task_dir.mkdir(parents=True, exist_ok=True)
    lock_path = task_dir / ".mas-task.lock"
    with lock_path.open("a+b") as handle:
        if os.name == "nt" and handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        _lock(handle, exclusive=exclusive)
        try:
            yield
        finally:
            _unlock(handle)
