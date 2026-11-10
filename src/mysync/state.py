"""Persistent synchronization state ("what both sides looked like after the
last successful sync").

Each side keeps its own state file, stored wherever that location's
``state_dir`` says: on this machine (default ``~/.config/mysync/db``) or inside
the location itself.  Both files carry the same session id; if they disagree,
or one is missing, the state is ignored and the next run behaves like a first
sync (nothing is deleted, differences become conflicts or copies).

Only paths that were identical on both sides are recorded, together with each
side's own view of the item (size and modification time as that side reports
them).  Each side's file uses that side's spelling of the names, which may
differ in case or Unicode form (see :mod:`mysync.names`).  Comparing the
current scan with this snapshot tells whether an item was created, modified
or deleted on a side since the last run.
"""

from __future__ import annotations

import gzip
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from mysync.backends.base import Backend
from mysync.config import JobConfig, LocationConfig
from mysync.model import Entry

FORMAT_VERSION = 1


@dataclass
class PairState:
    session: str
    timestamp: float
    entries: dict[str, tuple[Entry, Entry]] = field(default_factory=dict)
    # path -> (left name, right name) of items whose spelling may differ
    names: dict[str, tuple[str, str]] = field(default_factory=dict)
    raw: tuple[dict, dict] = ({}, {})  # each side's entries under its own names

    def join(self, key: Callable[[str], str]) -> None:
        """Pair the two sides by ``key`` instead of identical names; ``entries``
        and ``names`` are then indexed by key.  Ambiguous keys are dropped."""
        sides = []
        for raw in self.raw:
            found: dict[str, str] = {}
            clashes = set()
            for path in raw:
                k = key(path)
                if k in found:
                    clashes.add(k)
                found[k] = path
            sides.append({k: p for k, p in found.items() if k not in clashes})
        lkeys, rkeys = sides
        self.entries, self.names = {}, {}
        for k in lkeys.keys() & rkeys.keys():
            lp, rp = lkeys[k], rkeys[k]
            self.entries[k] = (Entry.from_list(self.raw[0][lp]), Entry.from_list(self.raw[1][rp]))
            self.names[k] = (lp, rp)


def state_file_name(job: JobConfig, side: str) -> str:
    return f"{job.pair_id}-{side}.json.gz"


def describe_location(loc: LocationConfig, job: JobConfig, side: str) -> str:
    name = state_file_name(job, side)
    if loc.state_in_location:
        return f"{loc.url} : {loc.state_dir}/{name}"
    return str(loc.local_state_dir / name)


def _read(loc: LocationConfig, backend: Backend, name: str) -> bytes | None:
    if loc.state_in_location:
        return backend.read_blob(f"{loc.state_dir}/{name}")
    try:
        return (loc.local_state_dir / name).read_bytes()
    except FileNotFoundError:
        return None


def _write(loc: LocationConfig, backend: Backend, name: str, data: bytes) -> None:
    if loc.state_in_location:
        backend.write_blob(f"{loc.state_dir}/{name}", data)
        return
    directory = loc.local_state_dir
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, directory / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _decode(raw: bytes | None) -> dict | None:
    if raw is None:
        return None
    try:
        data = json.loads(gzip.decompress(raw))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != FORMAT_VERSION:
        return None
    return data


@dataclass
class LoadInfo:
    state: PairState | None
    message: str


def load(job: JobConfig, left: Backend, right: Backend) -> LoadInfo:
    lraw = _decode(_read(job.left, left, state_file_name(job, "left")))
    rraw = _decode(_read(job.right, right, state_file_name(job, "right")))
    if lraw is None and rraw is None:
        return LoadInfo(None, "no previous synchronization state (first run)")
    if lraw is None or rraw is None:
        missing = "left" if lraw is None else "right"
        return LoadInfo(None, f"state of the {missing} side is missing; treating this as a first run")
    if lraw.get("session") != rraw.get("session"):
        return LoadInfo(None, "left and right state files are from different runs; treating this as a first run")
    lent, rent = lraw.get("entries", {}), rraw.get("entries", {})
    entries = {
        path: (Entry.from_list(lent[path]), Entry.from_list(rent[path]))
        for path in lent.keys() & rent.keys()
    }
    return LoadInfo(PairState(lraw["session"], float(lraw.get("time", 0)), entries, raw=(lent, rent)),
                    f"state from {time.strftime('%Y-%m-%d %H:%M', time.localtime(lraw.get('time', 0)))}")


def save(job: JobConfig, left: Backend, right: Backend, entries: dict[str, tuple[Entry, Entry]],
         real: tuple[Callable[[str], str], Callable[[str], str]] | None = None) -> None:
    """``real`` gives each side's spelling of the paths in ``entries``."""
    session = uuid.uuid4().hex
    now = time.time()
    for side, loc, backend, idx in (("left", job.left, left, 0), ("right", job.right, right, 1)):
        name = real[idx] if real else (lambda path: path)
        doc = {
            "version": FORMAT_VERSION,
            "session": session,
            "time": now,
            "job": job.name,
            "side": side,
            "left": job.left.url,
            "right": job.right.url,
            "entries": {name(path): pair[idx].to_list() for path, pair in entries.items()},
        }
        _write(loc, backend, state_file_name(job, side),
               gzip.compress(json.dumps(doc, separators=(",", ":")).encode(), mtime=0))


def last_sync_time(job: JobConfig) -> float | None:
    """Cheap lookup for the job list (only possible for locally stored state)."""
    for side, loc in (("left", job.left), ("right", job.right)):
        if not loc.state_in_location:
            data = _decode(_read(loc, None, state_file_name(job, side)))  # type: ignore[arg-type]
            if data:
                return float(data.get("time", 0))
    return None
