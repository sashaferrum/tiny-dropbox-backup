"""Exclusive installation lock."""

from __future__ import annotations

import os
from pathlib import Path


class LockError(RuntimeError):
    """Raised when another process owns the installation lock."""


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self._file = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._file = self.path.open("a+", encoding="utf-8")
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                try:
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    raise LockError(f"another dropbox-mirror process holds {self.path}") from error
            else:
                import fcntl

                try:
                    fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise LockError(f"another dropbox-mirror process holds {self.path}") from error
            os.chmod(self.path, 0o600)
        except Exception:
            if self._file is not None:
                self._file.close()
                self._file = None
            raise

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None

    def __enter__(self) -> "ProcessLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
