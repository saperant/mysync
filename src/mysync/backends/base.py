"""Backend interface: every storage type implements these primitives.

All paths handed to a backend are relative POSIX paths below the location
root ("" is the root itself).  Backends never need anything installed on the
remote side beyond what the protocol itself requires.

Backends must be safe to use from several threads at once: files are
transferred in parallel.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import posixpath
import re
import shlex
import time
from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import BinaryIO, Callable, Iterable, Iterator

from mysync.config import LocationConfig
from mysync.model import Entry

CHUNK = 1024 * 1024
TMP_SUFFIX = ".mysync-tmp"
#: checksum algorithms mysync can compare, best first
HASH_ALGOS = ("sha256", "sha1", "md5")

# excluded(rel, is_dir) -> bool
ExcludeFn = Callable[[str, bool], bool]
# called with the number of bytes transferred since the last call
BytesFn = Callable[[int], None]
# called with the number of items found so far while scanning
ScanProgressFn = Callable[[int], None]
# called with the number of files hashed so far
HashProgressFn = Callable[[int], None]
# (offset, data) pieces of a file
Blocks = Iterable[tuple[int, bytes]]

SYMLINK_MODES = ("skip", "follow", "links")


def join(*parts: str) -> str:
    return "/".join(p.strip("/") for p in parts if p and p.strip("/"))


def parent(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def tmp_name(rel: str) -> str:
    """Temporary sibling used for atomic writes."""
    head, _, tail = rel.rpartition("/")
    name = f".{tail}.{os.urandom(3).hex()}{TMP_SUFFIX}"
    return f"{head}/{name}" if head else name


def posix_hash_script(root_quoted: str, algo: str) -> str:
    """Shell script hashing NUL separated relative paths read from stdin.

    Prints one line per path, in input order: the checksum, or "-" when the
    file cannot be read.  File names are never printed, because tools escape
    them differently (FreeBSD's sha256sum writes "ä" as "\\303\\244").
    """
    loop = (f'for f; do h=$({algo}sum < "$f" 2>/dev/null) && [ -n "$h" ] '
            '&& printf "%s\\n" "${h%% *}" || echo -; done')
    return f"cd {root_quoted} && xargs -0 sh -c {shlex.quote(loop)} sh; exit 0"


def posix_hash_probe() -> str:
    """Prints the usable checksum tools, "no-xargs", "gnu-split" if ``split
    --filter`` works and "delta" if block patching works.  Never fails."""
    return (" ".join(f"command -v {a}sum >/dev/null 2>&1 && echo {a};" for a in HASH_ALGOS)
            + " command -v xargs >/dev/null 2>&1 || echo no-xargs;"
            + " split -b 1 --filter=cat /dev/null >/dev/null 2>&1 && echo gnu-split;"
            + " command -v truncate >/dev/null && printf x | dd bs=1 count=1 iflag=fullblock"
            + " status=none >/dev/null 2>&1 && echo delta; true")


def posix_free_space_script(root_quoted: str) -> str:
    """Prints ``df -P`` output for the root, or its nearest existing parent."""
    return (f'p={root_quoted}; while [ ! -e "$p" ] && [ "$p" != / ]; do p=$(dirname -- "$p"); done; '
            'df -Pk -- "$p"')


def parse_free_space(out: bytes) -> int | None:
    """Available bytes from ``df -Pk`` output."""
    lines = out.decode(errors="replace").strip().splitlines()
    m = re.search(r"\s(\d+)\s+(\d+)\s+(\d+)\s+\d+%", lines[-1]) if lines else None
    return int(m.group(3)) * 1024 if m else None


def touch_time(mtime: float) -> str:
    """Modification time for ``touch -d``; GNU and BSD both accept it."""
    whole = math.floor(mtime)
    frac = min(round((mtime - whole) * 1e9), 999_999_999)
    stamp = datetime.fromtimestamp(whole, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{stamp}.{frac:09d}Z"


def link_inside(rel: str, target: str) -> bool:
    """A relative link whose target stays inside the synchronized tree."""
    if not target or target.startswith("/"):
        return False
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
    return resolved != ".." and not resolved.startswith("../") and resolved != "."


# -- block level delta transfer ----------------------------------------------
#
# Both sides hash the file in fixed size blocks; only blocks that differ are
# sent and written into a copy of the old file.  Remote hosts only need dd,
# truncate, cp and sha256sum (GNU coreutils or the BSD equivalents).


def delta_block_size(size: int) -> int:
    """About 1000 blocks per file, between 64 KiB and 8 MiB."""
    bs = 64 * 1024
    while bs < 8 * 1024 * 1024 and size // bs > 1024:
        bs *= 2
    return bs


def block_hashes_of(f: BinaryIO, bs: int) -> list[str]:
    out = []
    while chunk := f.read(bs):
        while len(chunk) < bs:  # pipes may return short reads
            more = f.read(bs - len(chunk))
            if not more:
                break
            chunk += more
        out.append(hashlib.sha256(chunk).hexdigest())
    return out


def block_digest(hashes: list[str]) -> str:
    """Checksum over the block list, in the format ``split --filter sha256sum`` prints."""
    return hashlib.sha256("".join(f"{h}  -\n" for h in hashes).encode()).hexdigest()


def posix_block_hash_script(path_q: str, bs: int, gnu_split: bool = True) -> str:
    """Print one ``<sha256>  -`` line per block of the file."""
    if gnu_split:
        return f"split -b {bs} --filter=sha256sum -- {path_q}"
    return (f'f={path_q}; s=$(wc -c < "$f") || exit 1; n=0; '
            f'while [ $((n * {bs})) -lt $s ]; do '
            f'h=$(dd if="$f" bs={bs} skip=$n count=1 status=none | sha256sum | cut -c1-64); '
            '[ -n "$h" ] || exit 1; printf "%s  -\n" "$h"; n=$((n + 1)); done')


def parse_block_hashes(out: bytes) -> list[str]:
    return [line.split()[0] for line in out.decode().splitlines() if line.strip()]


def posix_patch_script(path_q: str, tmp_q: str, size: int, mtime: float, mode: int | None,
                       backup_q: str | None, bs: int, digest: str, stat_script: str,
                       gnu_split: bool = True) -> str:
    """Script reading "offset length\\n<data>" records from stdin and patching a copy.

    Offsets are multiples of ``bs`` (whole blocks), so plain ``seek`` in
    ``obs`` units works with GNU and BSD dd alike.  The result is verified
    against the source's block hashes before it replaces the file.
    """
    steps = [
        f"d={path_q}; t={tmp_q}",
        "trap 'rm -f \"$t\"' EXIT",
        '{ cp --reflink=auto -- "$d" "$t" 2>/dev/null || cp -- "$d" "$t"; } || exit 1',
        f'while read off len; do dd of="$t" ibs="$len" obs={bs} seek=$((off / {bs})) count=1 '
        'iflag=fullblock conv=notrunc status=none || exit 1; done',
        f'truncate -s {size} "$t" || exit 1',
        f'[ "$({posix_block_hash_script(chr(34) + "$t" + chr(34), bs, gnu_split)} | sha256sum | cut -c1-64)"'
        f' = "{digest}" ] || {{ echo "verification of patched file failed" >&2; exit 1; }}',
    ]
    if mode is not None:
        steps.append(f'chmod {mode:o} "$t" || exit 1')
    steps.append(f'touch -m -d {touch_time(mtime)} "$t" || exit 1')
    if backup_q:
        steps.append(f'mkdir -p -- "$(dirname -- {backup_q})" && mv -- "$d" {backup_q} || exit 1')
    steps.append('mv -f -- "$t" "$d" || exit 1')
    steps.append(stat_script)
    return "\n".join(steps)


def patch_stream(blocks: Blocks, on_bytes: BytesFn | None) -> Iterator[bytes]:
    """Encode blocks for :func:`posix_patch_script`."""
    for offset, data in blocks:
        yield f"{offset} {len(data)}\n".encode() + data
        if on_bytes:
            on_bytes(len(data))


def parse_hash_output(out: bytes, paths: list[str]) -> dict[str, str]:
    """Pair the lines of :func:`posix_hash_script` with the paths it was given."""
    lines = out.decode(errors="replace").splitlines()
    if len(lines) != len(paths):
        return {}  # something went wrong: better no checksums than wrong ones
    return {path: line.strip().lower() for path, line in zip(paths, lines)
            if line.strip() not in ("", "-")}


def copy_stream(src: BinaryIO, dst: BinaryIO, on_bytes: BytesFn | None = None) -> int:
    total = 0
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            return total
        dst.write(chunk)
        total += len(chunk)
        if on_bytes:
            on_bytes(len(chunk))


class Backend(ABC):
    """A storage location."""

    #: modification times are only stored with this resolution (seconds)
    time_resolution: float = 1e-6

    def __init__(self, loc: LocationConfig):
        self.loc = loc

    @property
    def label(self) -> str:
        return self.loc.url

    def connect(self) -> None:  # noqa: B027 - optional hook
        """Open connections; called before any other operation."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections."""

    def __enter__(self) -> "Backend":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @abstractmethod
    def root_exists(self) -> bool: ...

    @abstractmethod
    def scan(self, excluded: ExcludeFn, symlinks: str,
             progress: ScanProgressFn | None = None) -> dict[str, Entry]:
        """Return every item below the root that is not excluded.

        ``symlinks``: "skip" ignores links, "follow" reports what they point to,
        "links" reports them as LINK entries with their target.
        """

    @abstractmethod
    def stat(self, rel: str, symlinks: str) -> Entry | None:
        """Current attributes of one item (None if missing), like :meth:`scan`."""

    @abstractmethod
    def open_read(self, rel: str) -> AbstractContextManager[BinaryIO]: ...

    @abstractmethod
    def write_file(self, rel: str, src: BinaryIO, mtime: float,
                   on_bytes: BytesFn | None = None, mode: int | None = None) -> Entry:
        """Atomically create or replace ``rel`` with the content of ``src``.

        Parent directories must already exist.  Sets the modification time (and
        the permission bits when ``mode`` is given) and returns the attributes
        of the written file.
        """

    @abstractmethod
    def set_attrs(self, rel: str, mtime: float | None, mode: int | None) -> Entry:
        """Change modification time and/or permissions of an existing file."""

    def write_link(self, rel: str, target: str) -> Entry:
        """Atomically create or replace a symbolic link."""
        raise OSError(f"{self.label} does not support symbolic links")

    @abstractmethod
    def mkdir(self, rel: str) -> Entry:
        """Create a directory (and missing parents); succeeds if it exists."""

    @abstractmethod
    def remove(self, rel: str, is_dir: bool) -> None:
        """Delete a file, or a directory with everything inside it."""

    @abstractmethod
    def move(self, src: str, dst: str) -> None:
        """Rename within the location; parent directories of ``dst`` are created.

        Raises FileExistsError instead of replacing an existing ``dst``.
        """

    @abstractmethod
    def list_names(self, rel: str) -> list[str]:
        """Names directly inside directory ``rel`` ([] if it does not exist)."""

    def case_insensitive(self) -> bool | None:
        """Whether the storage ignores the case of names; None when it cannot tell
        without looking at names (the engine then probes a scanned one)."""
        return None

    def free_space(self) -> int | None:
        """Bytes that can still be written below the root (None if unknown)."""
        return None

    def ensure_connected(self) -> None:  # noqa: B027 - optional hook
        """Reconnect if the connection was lost (called before retrying failed items)."""

    def hash_algos(self) -> set[str]:
        """Checksums this backend computes where the data lives (no download)."""
        return set()

    def hash_file(self, rel: str, algo: str = "sha256") -> str:
        """Checksum of the file content (streams the file when not native)."""
        digest = hashlib.new(algo)
        with self.open_read(rel) as f:
            while chunk := f.read(CHUNK):
                digest.update(chunk)
        return digest.hexdigest()

    def hash_many(self, rels: list[str], algo: str,
                  progress: HashProgressFn | None = None) -> dict[str, str]:
        """Checksums of several files; unreadable files are left out."""
        result: dict[str, str] = {}
        for n, rel in enumerate(rels, 1):
            try:
                result[rel] = self.hash_file(rel, algo)
            except OSError:
                pass
            if progress:
                progress(n)
        return result

    # -- delta transfer (optional) ------------------------------------------
    def supports_delta(self) -> bool:
        return False

    def block_hashes(self, rel: str, bs: int) -> list[str]:
        """SHA-256 of every ``bs`` sized block, computed where the data lives."""
        raise NotImplementedError

    def read_blocks(self, rel: str, ranges: list[tuple[int, int]]) -> Iterator[tuple[int, bytes]]:
        """Yield ``(offset, data)`` for the requested ``(offset, length)`` ranges."""
        with self.open_read(rel) as f:
            for offset, length in ranges:
                f.seek(offset)
                yield offset, f.read(length)

    def patch_file(self, rel: str, blocks: Blocks, size: int, mtime: float, mode: int | None,
                   backup: str | None, expect: tuple[int, str], on_bytes: BytesFn | None) -> Entry:
        """Write ``blocks`` into a copy of ``rel``, truncate it to ``size``, verify it
        against ``expect`` = (block size, :func:`block_digest`), move the old
        version to ``backup`` (if given) and replace the file atomically."""
        raise NotImplementedError

    def read_blob(self, rel: str) -> bytes | None:
        """Read a small file, or None if it does not exist."""
        try:
            with self.open_read(rel) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def write_blob(self, rel: str, data: bytes) -> None:
        if parent(rel):
            self.mkdir(parent(rel))
        self.write_file(rel, io.BytesIO(data), time.time())
