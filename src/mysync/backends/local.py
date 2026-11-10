"""Local file system backend."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import BinaryIO

from mysync.backends.base import (
    HASH_ALGOS, Backend, Blocks, BytesFn, ExcludeFn, ScanProgressFn, block_digest, block_hashes_of,
    copy_stream, parent, tmp_name,
)
from mysync.model import DIR, FILE, LINK, Entry


def _entry(st: os.stat_result, target: str | None = None) -> Entry:
    mtime = st.st_mtime_ns / 1e9
    if stat.S_ISDIR(st.st_mode):
        return Entry(DIR, 0, mtime)
    if stat.S_ISLNK(st.st_mode):
        return Entry(LINK, 0, mtime, target=target)
    if stat.S_ISREG(st.st_mode):
        return Entry(FILE, st.st_size, mtime, stat.S_IMODE(st.st_mode), f"{st.st_dev}:{st.st_ino}")
    return Entry("?", 0, mtime)


def _clone(src: Path, dst: Path) -> None:
    """Copy a file, sharing blocks where the file system supports it (btrfs, xfs)."""
    with open(src, "rb") as fsrc, open(dst, "xb") as fdst:
        try:
            remaining = os.fstat(fsrc.fileno()).st_size
            while remaining > 0:
                n = os.copy_file_range(fsrc.fileno(), fdst.fileno(), remaining)
                if n == 0:
                    break
                remaining -= n
            if remaining == 0:
                return
        except OSError:
            pass
        fsrc.seek(0)
        fdst.seek(0)
        fdst.truncate()
        shutil.copyfileobj(fsrc, fdst, 1024 * 1024)


class LocalBackend(Backend):
    def __init__(self, loc):
        super().__init__(loc)
        self.root = Path(loc.path)

    def _abs(self, rel: str) -> Path:
        return self.root / rel if rel else self.root

    def root_exists(self) -> bool:
        return self.root.is_dir()

    def _lookup(self, path: Path | str, symlinks: str) -> Entry | None:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            if symlinks == "skip":
                return None
            if symlinks == "links":
                return _entry(st, os.readlink(path))
            try:
                st = os.stat(path)
            except OSError:
                return None  # dangling link
        return _entry(st)

    def stat(self, rel: str, symlinks: str) -> Entry | None:
        try:
            return self._lookup(self._abs(rel), symlinks)
        except FileNotFoundError:
            return None

    def scan(self, excluded: ExcludeFn, symlinks: str,
             progress: ScanProgressFn | None = None) -> dict[str, Entry]:
        result: dict[str, Entry] = {}
        seen_dirs: set[tuple[int, int]] = set()
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            try:
                it = os.scandir(self._abs(rel_dir))
            except FileNotFoundError:
                continue
            with it:
                for de in it:
                    rel = f"{rel_dir}/{de.name}" if rel_dir else de.name
                    try:
                        entry = self._lookup(de.path, symlinks)
                    except FileNotFoundError:
                        continue
                    if entry is None or entry.kind not in (FILE, DIR, LINK):
                        continue  # skipped links, sockets, fifos, devices
                    is_dir = entry.kind == DIR
                    if excluded(rel, is_dir):
                        continue
                    result[rel] = entry
                    if is_dir:
                        st = os.stat(de.path)
                        ident = (st.st_dev, st.st_ino)
                        if ident not in seen_dirs:  # protects against symlink loops
                            seen_dirs.add(ident)
                            stack.append(rel)
                    if progress and len(result) % 500 == 0:
                        progress(len(result))
        if progress:
            progress(len(result))
        return result

    def open_read(self, rel: str) -> BinaryIO:
        return open(self._abs(rel), "rb")

    def write_file(self, rel: str, src: BinaryIO, mtime: float,
                   on_bytes: BytesFn | None = None, mode: int | None = None) -> Entry:
        target = self._abs(rel)
        tmp = self._abs(tmp_name(rel))
        try:
            with open(tmp, "xb") as dst:
                copy_stream(src, dst, on_bytes)
                dst.flush()
                os.fsync(dst.fileno())
            if mode is not None:
                os.chmod(tmp, mode)
            os.utime(tmp, ns=(int(mtime * 1e9), int(mtime * 1e9)))
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return _entry(os.stat(target))

    def write_link(self, rel: str, target: str) -> Entry:
        tmp = self._abs(tmp_name(rel))
        os.symlink(target, tmp)
        try:
            os.replace(tmp, self._abs(rel))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return self.stat(rel, "links")

    def set_attrs(self, rel: str, mtime: float | None, mode: int | None) -> Entry:
        path = self._abs(rel)
        if mode is not None:
            os.chmod(path, mode)
        if mtime is not None:
            os.utime(path, ns=(int(mtime * 1e9), int(mtime * 1e9)))
        return _entry(os.stat(path))

    def mkdir(self, rel: str) -> Entry:
        path = self._abs(rel)
        path.mkdir(parents=True, exist_ok=True)
        return _entry(os.stat(path))

    def remove(self, rel: str, is_dir: bool) -> None:
        path = self._abs(rel)
        if is_dir and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()

    def move(self, src: str, dst: str) -> None:
        if parent(dst):
            self._abs(parent(dst)).mkdir(parents=True, exist_ok=True)
        if os.path.lexists(self._abs(dst)):
            raise FileExistsError(dst)
        os.rename(self._abs(src), self._abs(dst))

    def free_space(self) -> int | None:
        path = self.root
        while not path.exists() and path != path.parent:  # not created yet
            path = path.parent
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def list_names(self, rel: str) -> list[str]:
        try:
            return os.listdir(self._abs(rel))
        except FileNotFoundError:
            return []

    def hash_algos(self) -> set[str]:
        return set(HASH_ALGOS)

    # -- delta transfer -----------------------------------------------------
    def supports_delta(self) -> bool:
        return True

    def block_hashes(self, rel: str, bs: int) -> list[str]:
        with open(self._abs(rel), "rb") as f:
            return block_hashes_of(f, bs)

    def patch_file(self, rel: str, blocks: Blocks, size: int, mtime: float, mode: int | None,
                   backup: str | None, expect: tuple[int, str], on_bytes: BytesFn | None) -> Entry:
        target = self._abs(rel)
        tmp = self._abs(tmp_name(rel))
        try:
            _clone(target, tmp)
            with open(tmp, "r+b") as f:
                for offset, data in blocks:
                    f.seek(offset)
                    f.write(data)
                    if on_bytes:
                        on_bytes(len(data))
                f.truncate(size)
                f.flush()
                os.fsync(f.fileno())
                f.seek(0)
                if block_digest(block_hashes_of(f, expect[0])) != expect[1]:
                    raise OSError("verification of patched file failed")
            if mode is not None:
                os.chmod(tmp, mode)
            os.utime(tmp, ns=(int(mtime * 1e9), int(mtime * 1e9)))
            if backup:
                self.move(rel, backup)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return _entry(os.stat(target))
