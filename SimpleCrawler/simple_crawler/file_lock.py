"""Cross-platform process file locks for SimpleCrawler runtimes."""

from pathlib import Path
from typing import Optional, TextIO

import portalocker


class FileAlreadyLocked(RuntimeError):
    """Raised when an exclusive file lock cannot be acquired in time."""


class ExclusiveFileLock:
    """Own an exclusive process lock backed by portalocker."""

    def __init__(self, path: Path, *, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self._lock: Optional[portalocker.Lock] = None
        self._file: Optional[TextIO] = None

    def acquire(self) -> TextIO:
        if self._file is not None:
            return self._file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = portalocker.Lock(
            str(self.path),
            mode="a+",
            timeout=self.timeout,
            flags=(
                portalocker.LockFlags.EXCLUSIVE
                | portalocker.LockFlags.NON_BLOCKING
            ),
            encoding="utf-8",
        )
        try:
            lock_file = lock.acquire()
        except portalocker.exceptions.AlreadyLocked as error:
            raise FileAlreadyLocked(str(self.path)) from error
        self._lock = lock
        self._file = lock_file
        return lock_file

    def release(self) -> None:
        if self._lock is None:
            return
        self._lock.release()
        self._lock = None
        self._file = None

    def __enter__(self) -> TextIO:
        return self.acquire()

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
