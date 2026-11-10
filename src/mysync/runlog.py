"""Per-run log files in ~/.local/state/mysync/logs/."""

from __future__ import annotations

import re
import time
from pathlib import Path

from mysync import paths


class RunLog:
    def __init__(self, job_name: str, keep: int = 50):
        directory = paths.log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", job_name)
        self.path: Path = directory / f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.log"
        self._f = self.path.open("a", encoding="utf-8", errors="replace")
        old = sorted(directory.glob(f"{safe}-*.log"))
        for stale in old[:-keep]:
            stale.unlink(missing_ok=True)

    def write(self, message: str) -> None:
        self._f.write(f"{time.strftime('%H:%M:%S')} {message}\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()
