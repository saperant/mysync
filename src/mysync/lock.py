"""Per-job locks, so two runs of the same folder pair never overlap.

The lock is an flock()ed file in ``$XDG_RUNTIME_DIR/mysync`` (or the state
directory).  The kernel releases it if the process dies, and the file is
removed whenever the run ends, successfully or not.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from mysync import paths
from mysync.config import JobConfig
from mysync.model import SyncError


def lock_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "mysync" if runtime else paths.default_state_dir() / "locks"


class JobLock:
    def __init__(self, job: JobConfig, purpose: str = "sync"):
        self.job = job
        self.purpose = purpose
        self.path = lock_dir() / f"{job.pair_id}.lock"
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        while True:
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                holder = os.read(fd, 200).decode(errors="replace").strip()
                os.close(fd)
                raise SyncError(f"job {self.job.name!r} is already running"
                                + (f" ({holder})" if holder else "")) from None
            # the previous holder may have removed the file between our open and flock
            try:
                same = os.fstat(fd).st_ino == os.stat(self.path).st_ino
            except FileNotFoundError:
                same = False
            if same:
                break
            os.close(fd)
        os.ftruncate(fd, 0)
        os.write(fd, f"{self.purpose} by pid {os.getpid()}".encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            self.path.unlink(missing_ok=True)  # while still holding the lock
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "JobLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
