"""Pairing item names between two locations that spell them differently.

Storage differs in how it treats names: OneDrive, Dropbox, exFAT/NTFS disks
and macOS ignore case ("Foo.txt" is "foo.txt"), and macOS writes "é"
decomposed (e + combining accent) where Linux usually has it composed.

Items are paired by a key built from their names.  Every item gets one shared
path (the left's spelling, the right's for items only on the right), and each
side keeps its own spelling: a :class:`PathMap` translates shared paths into
the names a side really has, and :class:`MappedBackend` applies it to every
backend call, so the engine works with shared paths only.
"""

from __future__ import annotations

import posixpath
import unicodedata
from typing import Callable

from mysync.backends.base import Backend
from mysync.model import FILE, Entry


class NameMatcher:
    def __init__(self, case_sensitive: bool, unicode: bool):
        self.case_sensitive = case_sensitive
        self.unicode = unicode

    @property
    def exact(self) -> bool:
        return self.case_sensitive and not self.unicode

    def key(self, path: str) -> str:
        if self.unicode:
            path = unicodedata.normalize("NFC", path)
        if not self.case_sensitive:
            path = path.lower()
        return path


class PathMap:
    """How one side spells shared paths."""

    def __init__(self) -> None:
        # shared path -> this side's name of its last component, where they differ
        self.names: dict[str, str] = {}

    def name(self, path: str) -> str:
        return self.names.get(path, posixpath.basename(path))

    def real(self, path: str) -> str:
        if not self.names or not path:
            return path
        out = []
        prefix = ""
        for part in path.split("/"):
            prefix = f"{prefix}/{part}" if prefix else part
            out.append(self.names.get(prefix, part))
        return "/".join(out)

    def set_name(self, path: str, name: str) -> None:
        if name == posixpath.basename(path):
            self.names.pop(path, None)
        else:
            self.names[path] = name


class MappedBackend:
    """A backend addressed with shared paths."""

    def __init__(self, backend: Backend, pmap: PathMap):
        self.raw = backend
        self.map = pmap

    def __getattr__(self, name: str):
        return getattr(self.raw, name)

    def stat(self, rel: str, symlinks: str) -> Entry | None:
        return self.raw.stat(self.map.real(rel), symlinks)

    def open_read(self, rel: str):
        return self.raw.open_read(self.map.real(rel))

    def write_file(self, rel: str, *args, **kwargs) -> Entry:
        return self.raw.write_file(self.map.real(rel), *args, **kwargs)

    def write_link(self, rel: str, target: str) -> Entry:
        return self.raw.write_link(self.map.real(rel), target)

    def set_attrs(self, rel: str, mtime: float | None, mode: int | None) -> Entry:
        return self.raw.set_attrs(self.map.real(rel), mtime, mode)

    def mkdir(self, rel: str) -> Entry:
        return self.raw.mkdir(self.map.real(rel))

    def remove(self, rel: str, is_dir: bool) -> None:
        self.raw.remove(self.map.real(rel), is_dir)

    def move(self, src: str, dst: str) -> None:
        self.raw.move(self.map.real(src), self.map.real(dst))

    def list_names(self, rel: str) -> list[str]:
        return self.raw.list_names(self.map.real(rel))

    def hash_file(self, rel: str, algo: str = "sha256") -> str:
        return self.raw.hash_file(self.map.real(rel), algo)

    def hash_many(self, rels: list[str], algo: str, progress=None) -> dict[str, str]:
        real = {self.map.real(r): r for r in rels}
        return {real[k]: v for k, v in self.raw.hash_many(list(real), algo, progress).items()}

    def block_hashes(self, rel: str, bs: int) -> list[str]:
        return self.raw.block_hashes(self.map.real(rel), bs)

    def read_blocks(self, rel: str, ranges):
        return self.raw.read_blocks(self.map.real(rel), ranges)

    def patch_file(self, rel: str, blocks, size, mtime, mode, backup, expect, on_bytes) -> Entry:
        return self.raw.patch_file(self.map.real(rel), blocks, size, mtime, mode,
                                   self.map.real(backup) if backup else None, expect, on_bytes)


def mapped(backend: Backend | MappedBackend, pmap: PathMap) -> MappedBackend:
    return MappedBackend(getattr(backend, "raw", backend), pmap)


def detect_case_insensitive(backend: Backend, scanned: dict[str, Entry]) -> bool:
    """Does the storage ignore case?  Asks the backend, otherwise looks up a
    scanned name with its case swapped: finding the same item means yes."""
    native = backend.case_insensitive()
    if native is not None:
        return native
    candidates = (rel for rel in scanned
                  if any(c.isascii() and c.isalpha() for c in posixpath.basename(rel)))
    for rel in sorted(candidates, key=lambda r: (r.count("/"), r))[:1]:
        head, _, name = rel.rpartition("/")
        swapped = f"{head}/{name.swapcase()}" if head else name.swapcase()
        if swapped in scanned:
            return False
        found = backend.stat(swapped, "links")
        entry = scanned[rel]
        if found is None or found.kind != entry.kind:
            return False
        return entry.kind != FILE or (found.size == entry.size and abs(found.mtime - entry.mtime) < 1e-3)
    return False


def _depth(path: str) -> int:
    return path.count("/")


def pair_names(lscan: dict[str, Entry], rscan: dict[str, Entry], key: Callable[[str], str]
               ) -> tuple[dict[str, Entry], dict[str, Entry], PathMap, PathMap, dict[str, str], list[str]]:
    """Re-key both scans by shared path.

    Returns (left, right, left map, right map, key -> shared path, warnings).
    Names that collide on one side (e.g. "a.txt" and "A.txt" next to each
    other while case is ignored) cannot be paired: they are left alone on
    both sides, together with everything inside them, like excluded items.
    """
    warnings: list[str] = []
    by_key: list[dict[str, str]] = []
    clashes: set[str] = set()
    for side, scan in (("left", lscan), ("right", rscan)):
        keys: dict[str, str] = {}
        groups: dict[str, list[str]] = {}
        for path in scan:
            k = key(path)
            if k in keys:
                groups.setdefault(k, [keys[k]]).append(path)
            keys[k] = path
        for k, paths in sorted(groups.items()):
            clashes.add(k)
            warnings.append(f"names on the {side} differ only in case or Unicode form: "
                            f"{', '.join(sorted(paths))} - left alone on both sides")
        by_key.append(keys)
    lkeys, rkeys = by_key
    if clashes:
        prefixes = tuple(k + "/" for k in clashes)
        for keys in (lkeys, rkeys):
            for k in [k for k in keys if k in clashes or k.startswith(prefixes)]:
                del keys[k]

    shared: dict[str, str] = {}
    lmap, rmap = PathMap(), PathMap()
    left: dict[str, Entry] = {}
    right: dict[str, Entry] = {}
    for k in sorted(lkeys.keys() | rkeys.keys(), key=_depth):
        lpath, rpath = lkeys.get(k), rkeys.get(k)
        own = lpath if lpath is not None else rpath
        head = k.rpartition("/")[0]
        name = posixpath.basename(own)
        path = f"{shared.get(head, posixpath.dirname(own))}/{name}" if head else name
        shared[k] = path
        if lpath is not None:
            left[path] = lscan[lpath]
            lmap.set_name(path, posixpath.basename(lpath))
        if rpath is not None:
            right[path] = rscan[rpath]
            rmap.set_name(path, posixpath.basename(rpath))
    return left, right, lmap, rmap, shared, warnings
