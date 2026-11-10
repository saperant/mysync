from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from mysync.backends import open_backend
from mysync.compare import compare
from mysync.config import job_from_dict
from mysync.sync import synchronize


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MYSYNC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("MYSYNC_LOG_DIR", str(tmp_path / "logs"))


def write(path: Path, text: str, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def tree(root: Path) -> dict[str, str]:
    """Map of relative path -> content ("/" for folders), ignoring .mysync."""
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel == ".mysync" or rel.startswith(".mysync/"):
            continue
        out[rel] = "/" if p.is_dir() else p.read_text()
    return out


class Pair:
    """A job between two local folders, with helpers to run it."""

    def __init__(self, tmp_path: Path, left_url: str | None = None, right_url: str | None = None, **opts):
        self.left = tmp_path / "left"
        self.right = tmp_path / "right"
        self.left.mkdir(exist_ok=True)
        self.right.mkdir(exist_ok=True)
        data = {"left": left_url or str(self.left), "right": right_url or str(self.right),
                "retries": 0}  # tests of failures should not wait
        data.update(opts)
        self.job = job_from_dict(data, "test")

    def compare(self):
        with open_backend(self.job.left) as lb, open_backend(self.job.right) as rb:
            return compare(self.job, lb, rb)

    def sync(self, force: bool = False, adjust=None):
        with open_backend(self.job.left) as lb, open_backend(self.job.right) as rb:
            cmp = compare(self.job, lb, rb)
            if adjust:
                adjust(cmp)
            result = synchronize(cmp, lb, rb, force=force)
            return cmp, result


@pytest.fixture
def pair(tmp_path):
    def make(**opts) -> Pair:
        return Pair(tmp_path, **opts)
    return make


@pytest.fixture
def past():
    """A timestamp safely in the past (whole seconds)."""
    return float(int(time.time()) - 3600)
