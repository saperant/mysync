"""SSH backend using the system OpenSSH client and standard remote tools.

Nothing has to be installed on the remote host apart from a POSIX shell and
the standard tools of the system: GNU findutils/coreutils on Linux (find
-printf), or the base system of FreeBSD and similar (find, stat -f).
Checksums need ``sha256sum`` (also on FreeBSD 14+).  Connections are
multiplexed with an OpenSSH control master, and ``~/.ssh/config`` applies as
usual.  Every operation is an independent ssh invocation, so the backend can
be used from several threads.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from typing import BinaryIO, Iterable, Iterator

from mysync.backends.base import (
    CHUNK, Backend, Blocks, BytesFn, ExcludeFn, HashProgressFn, ScanProgressFn, parent,
    parse_block_hashes, parse_free_space, parse_hash_output, patch_stream, posix_block_hash_script,
    posix_free_space_script, posix_hash_probe, posix_hash_script, posix_patch_script, tmp_name,
    touch_time,
)
from mysync.model import DIR, FILE, LINK, Entry, SyncError

_MISSING = 44  # exit status used by our scripts for "no such file"
_EXISTS = 45  # ... and for "target already exists"
# type, size, mtime, permissions, device:inode, path; then the link target
_FORMAT = "\\t%s\\t%T@\\t%m\\t%D:%i\\t%P\\0%l\\0"


def _q(path: str) -> str:
    return shlex.quote(path)


def _parse_entry(kind: str, size: str, mtime: str, mode: str, fid: str, target: str) -> Entry | None:
    if kind == "d":
        return Entry(DIR, 0, float(mtime))
    if kind == "f":
        return Entry(FILE, int(size), float(mtime), int(mode, 8), fid)
    if kind == "l":
        return Entry(LINK, 0, float(mtime), target=target)
    return None


def _parse_records(out: bytes) -> Iterator[tuple[str, Entry | None]]:
    tokens = out.decode(errors="surrogateescape").split("\0")
    for i in range(0, len(tokens) - 1, 2):
        kind, size, mtime, mode, fid, rel = tokens[i].split("\t", 5)
        yield rel, _parse_entry(kind, size, mtime, mode, fid, tokens[i + 1])


def _find_args(symlinks: str) -> tuple[str, str]:
    """find option and type directive for a symlink policy."""
    return ("-L ", "%Y") if symlinks == "follow" else ("", "%y")


# BSD stat(1): one line per item; the name comes last so tabs in it are harmless
_BSD_FORMAT = "%HT\t%z\t%Fm\t%OLp\t%d:%i\t%N"
_BSD_TYPES = {"Regular File": "f", "Directory": "d", "Symbolic Link": "l"}
_BSD_KNOWN = ("Regular File\t", "Directory\t", "Symbolic Link\t", "Fifo File\t", "Socket\t",
              "Character Device\t", "Block Device\t", "Whiteout\t", "Unknown\t")


def _parse_bsd_records(out: bytes) -> Iterator[tuple[str, Entry | None]]:
    """Parse BSD stat lines; a line that does not start a record continues
    the previous name (a file name containing a newline)."""
    records: list[str] = []
    text = out.decode(errors="surrogateescape")
    for line in (text[:-1] if text.endswith("\n") else text).split("\n"):
        if line.startswith(_BSD_KNOWN) and line.count("\t") >= 5:
            records.append(line)
        elif records:
            records[-1] += "\n" + line
    for rec in records:
        kind, size, mtime, mode, fid, rel = rec.split("\t", 5)
        if rel.startswith("./"):
            rel = rel[2:]
        yield rel, _parse_entry(_BSD_TYPES.get(kind, "?"), size, mtime, mode, fid, "")


class _RemoteReader:
    """File-like object streaming ``cat`` output of a remote file."""

    def __init__(self, proc: subprocess.Popen, rel: str):
        self.proc = proc
        self.rel = rel

    def read(self, n: int = -1) -> bytes:
        return self.proc.stdout.read(n)

    def __enter__(self) -> "_RemoteReader":
        return self

    def __exit__(self, exc_type, *exc) -> None:
        if exc_type is not None:
            self.proc.kill()
        self.proc.stdout.close()
        rc = self.proc.wait()
        err = self.proc.stderr.read().decode(errors="replace").strip()
        self.proc.stderr.close()
        if exc_type is None:
            if rc == _MISSING:
                raise FileNotFoundError(self.rel)
            if rc != 0:
                raise OSError(f"reading {self.rel} failed: {err or f'exit status {rc}'}")


class SshBackend(Backend):
    def __init__(self, loc):
        super().__init__(loc)
        self.root = loc.path
        self._control_dir: str | None = None
        self._base: list[str] = []
        self._algos: set[str] = set()
        self._delta = False
        self._gnu = True  # GNU find/coreutils, otherwise BSD tools
        self._gnu_split = True

    # -- connection -------------------------------------------------------
    def connect(self) -> None:
        opts = self.loc.options
        cmd = opts.get("ssh_command", ["ssh"])
        cmd = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
        if not shutil.which(cmd[0]):
            raise SyncError(f"{self.label}: '{cmd[0]}' not found")
        # control sockets need a short path
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        self._control_dir = tempfile.mkdtemp(prefix="mysync-ssh-",
                                             dir=runtime if runtime and os.path.isdir(runtime) else None)
        base = cmd + [
            "-o", "BatchMode=yes",
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_dir}/cm",
            "-o", "ControlPersist=300",
            "-o", f"ConnectTimeout={int(opts.get('connect_timeout', 15))}",
        ]
        policy = opts.get("host_key_policy", "strict")
        if policy not in ("strict", "accept-new"):
            raise SyncError(f"{self.label}: host_key_policy must be 'strict' or 'accept-new'")
        base += ["-o", "StrictHostKeyChecking=" + ("yes" if policy == "strict" else "accept-new")]
        if opts.get("known_hosts"):
            base += ["-o", f"UserKnownHostsFile={opts['known_hosts']}"]
        if opts.get("identity_file"):
            base += ["-i", str(opts["identity_file"])]
        for opt in opts.get("ssh_options", []):
            base += ["-o", opt]
        if self.loc.port:
            base += ["-p", str(self.loc.port)]
        if self.loc.user:
            base += ["-l", self.loc.user]
        self._base = base + ["--", self.loc.host]

        _, out = self._run('printf "%s\\n" "$HOME"; '
                           # FreeBSD 15 find has a partial -printf: insist on GNU find
                           'if find --version 2>/dev/null | grep -q GNU; then echo gnu; '
                           'elif stat -f %z / >/dev/null 2>&1; then echo bsd; else echo unknown; fi; '
                           + posix_hash_probe())
        lines = out.decode(errors="surrogateescape").splitlines()
        if len(lines) < 2 or lines[1] not in ("gnu", "bsd"):
            raise SyncError(f"{self.label}: the remote system has neither GNU 'find -printf' nor BSD "
                            "'stat -f'; use an sftp:// location instead")
        self._gnu = lines[1] == "gnu"
        self._gnu_split = "gnu-split" in lines
        if "no-xargs" not in lines:
            self._algos = set(lines[2:]) & {"sha256", "sha1", "md5"}
        self._delta = "delta" in lines and "sha256" in lines
        if self.root.startswith("~"):
            self.root = lines[0].rstrip("/") + "/" + self.root[1:].lstrip("/")
        self.root = self.root.rstrip("/") or "/"

    def close(self) -> None:
        if self._control_dir:
            subprocess.run(self._base[:-2] + ["-O", "exit", "--", self.loc.host],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            shutil.rmtree(self._control_dir, ignore_errors=True)
            self._control_dir = None

    def _cmd(self, script: str) -> list[str]:
        return self._base + ["sh -c " + _q(script)]

    def _run(self, script: str, ok_codes: tuple[int, ...] = (0,)) -> tuple[int, bytes]:
        proc = subprocess.run(self._cmd(script), stdin=subprocess.DEVNULL,
                              capture_output=True, check=False)
        if proc.returncode == _MISSING and _MISSING not in ok_codes:
            raise FileNotFoundError(script)
        if proc.returncode not in ok_codes:
            err = proc.stderr.decode(errors="replace").strip()
            if proc.returncode == 255:
                raise SyncError(f"{self.label}: ssh failed: {err}")
            raise OSError(err or f"remote command failed with status {proc.returncode}")
        return proc.returncode, proc.stdout

    def _run_streaming(self, script: str, chunks: Iterable[bytes]) -> bytes:
        """Run a script feeding it ``chunks`` on stdin; returns stdout."""
        with tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as out:
            proc = subprocess.Popen(self._cmd(script), stdin=subprocess.PIPE, stdout=out, stderr=err)
            try:
                for chunk in chunks:
                    proc.stdin.write(chunk)
                proc.stdin.close()
            except BrokenPipeError:
                pass  # the script failed early; its status tells why
            except BaseException:
                proc.kill()
                proc.wait()
                raise
            rc = proc.wait()
            if rc != 0:
                err.seek(0)
                raise OSError(err.read().decode(errors="replace").strip() or f"status {rc}")
            out.seek(0)
            return out.read()

    def _abs(self, rel: str) -> str:
        return f"{self.root}/{rel}" if rel else self.root

    def _stat_script(self, var: str, symlinks: str = "skip") -> str:
        if not self._gnu:
            # type, size, mtime, mode, identity, link target (no name: printed by the caller)
            follow = "-L " if symlinks == "follow" else ""
            return f'stat {follow}-f "%HT\t%z\t%Fm\t%OLp\t%d:%i\t%Y" "{var}"'
        opt, kind = _find_args(symlinks)
        return f'find {opt}"{var}" -maxdepth 0 -printf "{kind}{_FORMAT}"'

    def _records(self, out: bytes) -> Iterator[tuple[str, Entry | None]]:
        if self._gnu:
            return _parse_records(out)
        return self._parse_bsd_stat(out)

    @staticmethod
    def _parse_bsd_stat(out: bytes) -> Iterator[tuple[str, Entry | None]]:
        """Output of :meth:`_stat_script` on BSD: the last field is the link target."""
        text = out.decode(errors="surrogateescape")
        if text.endswith("\n"):
            text = text[:-1]
        if not text:
            return
        kind, size, mtime, mode, fid, target = text.split("\t", 5)
        yield "", _parse_entry(_BSD_TYPES.get(kind, "?"), size, mtime, mode, fid, target)

    def _entry_from(self, out: bytes) -> Entry:
        for _, entry in self._records(out):
            if entry is not None:
                return entry
        raise OSError("unexpected item type")

    # -- operations -------------------------------------------------------
    def root_exists(self) -> bool:
        return self._run(f"test -d {_q(self.root)} && echo y || true")[1].strip() == b"y"

    def stat(self, rel: str, symlinks: str) -> Entry | None:
        _, out = self._run(f'd={_q(self._abs(rel))}; if [ ! -e "$d" ] && [ ! -L "$d" ]; then exit 0; fi; '
                           + self._stat_script("$d", symlinks))
        for _, entry in self._records(out):
            if entry is not None and not (entry.kind == LINK and symlinks == "skip"):
                return entry
        return None

    def scan(self, excluded: ExcludeFn, symlinks: str,
             progress: ScanProgressFn | None = None) -> dict[str, Entry]:
        opt, kind = _find_args(symlinks)
        if self._gnu:
            script = (f"cd {_q(self.root)} 2>/dev/null || exit 0; "
                      f"exec find {opt}. -mindepth 1 -printf '{kind}{_FORMAT}'")
            records = _parse_records(self._run(script)[1])
        else:
            follow = "-L " if symlinks == "follow" else ""
            script = (f"cd {_q(self.root)} 2>/dev/null || exit 0; "
                      f"exec find {opt}. -mindepth 1 -exec stat {follow}-f '{_BSD_FORMAT}' {{}} +")
            records = _parse_bsd_records(self._run(script)[1])
        result: dict[str, Entry] = {}
        pruned: list[str] = []
        for rel, entry in records:
            if entry is None or (entry.kind == LINK and symlinks != "links"):
                continue
            if pruned and any(rel.startswith(p) for p in pruned):
                continue
            if excluded(rel, entry.kind == DIR):
                if entry.kind == DIR:
                    pruned.append(rel + "/")
                continue
            result[rel] = entry
            if progress and len(result) % 500 == 0:
                progress(len(result))
        if not self._gnu and symlinks == "links":
            self._read_link_targets(result)
        if progress:
            progress(len(result))
        return result

    def _read_link_targets(self, entries: dict[str, Entry]) -> None:
        links = [rel for rel, e in entries.items() if e.kind == LINK]
        if not links:
            return
        script = (f"cd {_q(self.root)} && xargs -0 sh -c "
                  + _q('for f; do printf "%s\\0%s\\0" "$f" "$(readlink -- "$f")"; done') + " sh")
        out = self._run_streaming(script, [b"\0".join(r.encode(errors="surrogateescape") for r in links)
                                           + b"\0"])
        tokens = out.decode(errors="surrogateescape").split("\0")
        for i in range(0, len(tokens) - 1, 2):
            if tokens[i] in entries:
                entries[tokens[i]].target = tokens[i + 1]

    def open_read(self, rel: str) -> BinaryIO:
        path = _q(self._abs(rel))
        proc = subprocess.Popen(self._cmd(f"test -f {path} || exit {_MISSING}; exec cat -- {path}"),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return _RemoteReader(proc, rel)

    def write_file(self, rel: str, src: BinaryIO, mtime: float,
                   on_bytes: BytesFn | None = None, mode: int | None = None) -> Entry:
        chmod = f'chmod {mode:o} "$t" && ' if mode is not None else ""
        script = (
            f't={_q(self._abs(tmp_name(rel)))}; d={_q(self._abs(rel))}; '
            'trap \'rm -f "$t"\' EXIT; '
            f'cat > "$t" && {chmod}touch -m -d {touch_time(mtime)} "$t" && mv -f "$t" "$d" && '
            + self._stat_script("$d")
        )

        def chunks() -> Iterator[bytes]:
            while chunk := src.read(CHUNK):
                yield chunk
                if on_bytes:
                    on_bytes(len(chunk))
        try:
            out = self._run_streaming(script, chunks())
        except OSError as e:
            raise OSError(f"writing {rel} failed: {e}") from e
        return self._entry_from(out)

    def write_link(self, rel: str, target: str) -> Entry:
        script = (f't={_q(self._abs(tmp_name(rel)))}; d={_q(self._abs(rel))}; '
                  f'ln -s -- {_q(target)} "$t" && mv -f {"-T" if self._gnu else "-h"} -- "$t" "$d" && '
                  + self._stat_script("$d", "links"))
        return self._entry_from(self._run(script)[1])

    def set_attrs(self, rel: str, mtime: float | None, mode: int | None) -> Entry:
        steps = [f"d={_q(self._abs(rel))}"]
        if mode is not None:
            steps.append(f'chmod {mode:o} "$d"')
        if mtime is not None:
            steps.append(f'touch -m -c -d {touch_time(mtime)} "$d"')
        return self._entry_from(self._run(" && ".join(steps + [self._stat_script("$d")]))[1])

    def mkdir(self, rel: str) -> Entry:
        path = _q(self._abs(rel))
        return self._entry_from(self._run(f"d={path}; mkdir -p -- \"$d\" && " + self._stat_script("$d"))[1])

    def remove(self, rel: str, is_dir: bool) -> None:
        path = _q(self._abs(rel))
        self._run(f"rm -{'r' if is_dir else ''}f -- {path}")

    def move(self, src: str, dst: str) -> None:
        mk = f"mkdir -p -- {_q(self._abs(parent(dst)))} && " if parent(dst) else ""
        target = _q(self._abs(dst))
        rc, _ = self._run(f"if [ -e {target} ] || [ -L {target} ]; then exit {_EXISTS}; fi; "
                          f"{mk}mv -- {_q(self._abs(src))} {target}", ok_codes=(0, _EXISTS))
        if rc == _EXISTS:
            raise FileExistsError(dst)

    def free_space(self) -> int | None:
        try:
            return parse_free_space(self._run(posix_free_space_script(_q(self.root)))[1])
        except OSError:
            return None

    def list_names(self, rel: str) -> list[str]:
        path = _q(self._abs(rel))
        _, out = self._run(f"test -d {path} || exit 0; find {path} -mindepth 1 -maxdepth 1 -print0")
        return [n.rsplit("/", 1)[-1] for n in out.decode(errors="surrogateescape").split("\0") if n]

    def hash_algos(self) -> set[str]:
        return set(self._algos)

    def hash_many(self, rels: list[str], algo: str,
                  progress: HashProgressFn | None = None) -> dict[str, str]:
        if algo not in self._algos:
            return super().hash_many(rels, algo, progress)
        result: dict[str, str] = {}
        for start in range(0, len(rels), 2000):
            batch = rels[start:start + 2000]
            proc = subprocess.run(self._cmd(posix_hash_script(_q(self.root), algo)),
                                  input="\0".join(batch).encode(errors="surrogateescape") + b"\0",
                                  capture_output=True, check=False)
            if proc.returncode == 255:
                raise SyncError(f"{self.label}: ssh failed: {proc.stderr.decode(errors='replace')}")
            result.update(parse_hash_output(proc.stdout, batch))
            if progress:
                progress(start + len(batch))
        return result

    def hash_file(self, rel: str, algo: str = "sha256") -> str:
        if algo in self._algos:
            found = self.hash_many([rel], algo)
            if rel in found:
                return found[rel]
            raise FileNotFoundError(rel)
        return super().hash_file(rel, algo)

    # -- delta transfer -----------------------------------------------------
    def supports_delta(self) -> bool:
        return self._delta

    def block_hashes(self, rel: str, bs: int) -> list[str]:
        return parse_block_hashes(self._run(posix_block_hash_script(_q(self._abs(rel)), bs,
                                                                    self._gnu_split))[1])

    def read_blocks(self, rel: str, ranges: list[tuple[int, int]]) -> Iterator[tuple[int, bytes]]:
        path = _q(self._abs(rel))
        for start in range(0, len(ranges), 200):
            batch = ranges[start:start + 200]
            # offsets are whole delta blocks (multiples of 64 KiB): plain dd works everywhere
            script = "; ".join(
                f"dd if={path} bs=65536 skip={off // 65536} count={-(-length // 65536)} status=none"
                f" | head -c {length}" if off % 65536 == 0 else
                f"dd if={path} bs=65536 iflag=skip_bytes,count_bytes skip={off} count={length} status=none"
                for off, length in batch)
            proc = subprocess.Popen(self._cmd(script), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                for off, length in batch:
                    data = proc.stdout.read(length)
                    if len(data) != length:
                        raise OSError(f"reading blocks of {rel} failed: "
                                      f"{proc.stderr.read().decode(errors='replace').strip()}")
                    yield off, data
            finally:
                proc.stdout.close()
                if proc.poll() is None:
                    proc.kill()
                proc.wait()
                proc.stderr.close()

    def patch_file(self, rel: str, blocks: Blocks, size: int, mtime: float, mode: int | None,
                   backup: str | None, expect: tuple[int, str], on_bytes: BytesFn | None) -> Entry:
        script = posix_patch_script(
            _q(self._abs(rel)), _q(self._abs(tmp_name(rel))), size, mtime, mode,
            _q(self._abs(backup)) if backup else None, expect[0], expect[1], self._stat_script("$d"),
            self._gnu_split)
        try:
            out = self._run_streaming(script, patch_stream(blocks, on_bytes))
        except OSError as e:
            raise OSError(f"patching {rel} failed: {e}") from e
        return self._entry_from(out)

