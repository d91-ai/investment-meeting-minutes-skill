#!/usr/bin/env python3
"""Cross-platform advisory file locking: POSIX fcntl.flock, Windows msvcrt fallback."""

from __future__ import annotations

import os
import sys
import time

if sys.platform == "win32":
    import msvcrt

    LOCK_SH = 1
    LOCK_EX = 2
    LOCK_UN = 8

    def flock(fd: int, operation: int) -> None:
        """Blocking byte-range lock; shared locks degrade to exclusive on Windows."""
        if operation == LOCK_UN:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            return
        while True:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)
else:
    import fcntl

    LOCK_SH = fcntl.LOCK_SH
    LOCK_EX = fcntl.LOCK_EX
    LOCK_UN = fcntl.LOCK_UN

    def flock(fd: int, operation: int) -> None:
        fcntl.flock(fd, operation)
