"""Backend for any remote supported by rclone (Google Drive, OneDrive, Dropbox...).

rclone is used purely as a transport: listing, uploading, downloading and
deleting single items.  All synchronization decisions and state are mysync's.
Configure remotes with ``rclone config`` first, then use ``rclone:remote:path``.

One ``rclone rcd`` process is started per location and kept running; it is
reached through a unix socket in a private directory, so no network port or
password is involved, and authentication/token refreshes happen only once.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import BinaryIO
from urllib.parse import quote

from mysync.backends.base import (
    HASH_ALGOS, Backend, BytesFn, ExcludeFn, HashProgressFn, ScanProgressFn, copy_stream,
)
from mysync.model import DIR, FILE, Entry, SyncError

_TIME_RE = re.compile(r"^(.*T\d\d:\d\d:\d\d)(?:\.(\d+))?(.*)$")
_NOT_FOUND_RE = re.compile(r"not found|doesn't exist|does not exist|no such file", re.IGNORECASE)


def parse_time(value: str) -> float:
    """Parse rclone's RFC 3339 times, keeping sub-microsecond digits."""
    m = _TIME_RE.match(value)
    if not m:
        return datetime.fromisoformat(value).timestamp()
    base, frac, tz = m.groups()
    if tz in ("Z", "z"):
        tz = "+00:00"
    seconds = datetime.fromisoformat(base + tz).timestamp()
    return seconds + (float("0." + frac) if frac else 0.0)


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float | None):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


class _Reader:
    """Streams a file served by rcd; closes the connection afterwards."""

    def __init__(self, conn: http.client.HTTPConnection, resp: http.client.HTTPResponse):
        self.conn = conn
        self.resp = resp

    def read(self, n: int = -1) -> bytes:
        return self.resp.read(None if n < 0 else n)

    def __enter__(self) -> "_Reader":
        return self

    def __exit__(self, *exc) -> None:
        self.conn.close()


class RcloneBackend(Backend):
    time_resolution = 1.0

    def __init__(self, loc):
        super().__init__(loc)
        self.root = loc.path.rstrip("/") if not loc.path.endswith(":") else loc.path
        self._cmd: list[str] = []
        self._tmpdir: str | None = None
        self._sock = ""
        self._proc: subprocess.Popen | None = None
        self._algos: set[str] | None = None
        self._fsinfo: dict | None = None

    # -- daemon -------------------------------------------------------------
    def connect(self) -> None:
        binary = os.path.expanduser(str(self.loc.options.get("rclone", "rclone")))
        if not shutil.which(binary):
            raise SyncError(f"{self.label}: rclone binary {binary!r} not found "
                            "(install rclone or set rclone = \"/path/to/rclone\")")
        flags = self.loc.options.get("rclone_flags", [])
        flags = shlex.split(flags) if isinstance(flags, str) else list(flags)
        self._cmd = [binary, *flags]
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        self._tmpdir = tempfile.mkdtemp(prefix="mysync-rclone-",
                                        dir=runtime if runtime and os.path.isdir(runtime) else None)
        self._sock = os.path.join(self._tmpdir, "rc.sock")
        log = open(os.path.join(self._tmpdir, "rcd.log"), "wb")
        links = "--copy-links" if self.loc.options.get("_symlinks") == "follow" else "--skip-links"
        self._proc = subprocess.Popen(
            [*self._cmd, "rcd", "--rc-addr", f"unix://{self._sock}", "--rc-no-auth", "--rc-serve", links],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        log.close()
        deadline = time.monotonic() + 20
        while True:
            if self._proc.poll() is not None:
                raise SyncError(f"{self.label}: rclone failed to start: {self._log_tail()}")
            if os.path.exists(self._sock):
                try:
                    self._call("core/version", {})
                    return
                except OSError:
                    pass
            if time.monotonic() > deadline:
                raise SyncError(f"{self.label}: rclone did not start: {self._log_tail()}")
            time.sleep(0.05)

    def _log_tail(self) -> str:
        try:
            with open(os.path.join(self._tmpdir, "rcd.log"), "rb") as f:
                return f.read().decode(errors="replace").strip().splitlines()[-1]
        except (OSError, IndexError):
            return "no output"

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._proc = None
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def _call(self, method: str, params: dict) -> dict:
        conn = _UnixConnection(self._sock, None)
        try:
            conn.request("POST", "/" + method, json.dumps(params),
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        try:
            data = json.loads(body) if body else {}
        except ValueError:
            data = {"error": body.decode(errors="replace")}
        if resp.status != 200:
            msg = str(data.get("error", resp.status))
            if resp.status == 404 or _NOT_FOUND_RE.search(msg):
                raise FileNotFoundError(f"{method}: {msg}")
            raise OSError(f"rclone {method}: {msg}")
        return data

    # -- helpers --------------------------------------------------------------
    @staticmethod
    def _entry(item: dict) -> Entry:
        mtime = parse_time(item["ModTime"])
        if item.get("IsDir"):
            return Entry(DIR, 0, mtime)
        return Entry(FILE, int(item["Size"]), mtime, None, item.get("ID"))

    def _p(self, rel: str) -> str:
        if not rel:
            return self.root
        return f"{self.root}{rel}" if self.root.endswith(":") else f"{self.root}/{rel}"

    # -- operations -------------------------------------------------------
    def root_exists(self) -> bool:
        try:
            self._call("operations/list", {"fs": self.root, "remote": "",
                                           "opt": {"noMimeType": True, "noModTime": True}})
            return True
        except FileNotFoundError:
            return False

    def stat(self, rel: str, symlinks: str) -> Entry | None:
        try:
            item = self._call("operations/stat", {"fs": self.root, "remote": rel,
                                                  "opt": {"noMimeType": True}}).get("item")
        except FileNotFoundError:
            return None
        return self._entry(item) if item else None

    def scan(self, excluded: ExcludeFn, symlinks: str,
             progress: ScanProgressFn | None = None) -> dict[str, Entry]:
        try:
            listing = self._call("operations/list", {
                "fs": self.root, "remote": "",
                "opt": {"recurse": True, "noMimeType": True},
            }).get("list") or []
        except FileNotFoundError:
            return {}
        result: dict[str, Entry] = {}
        pruned: list[str] = []
        for item in sorted(listing, key=lambda i: i["Path"]):
            rel = item["Path"]
            is_dir = bool(item.get("IsDir"))
            if pruned and any(rel.startswith(p) for p in pruned):
                continue
            if excluded(rel, is_dir):
                if is_dir:
                    pruned.append(rel + "/")
                continue
            result[rel] = self._entry(item)
        if progress:
            progress(len(result))
        return result

    def open_read(self, rel: str) -> BinaryIO:
        conn = _UnixConnection(self._sock, None)
        conn.request("GET", "/%5B" + quote(self.root, safe="") + "%5D/" + quote(rel))
        resp = conn.getresponse()
        if resp.status != 200:
            body = resp.read().decode(errors="replace")
            conn.close()
            if resp.status == 404:
                raise FileNotFoundError(rel)
            raise OSError(f"reading {rel} failed: {resp.status} {body[:200]}")
        return _Reader(conn, resp)

    def write_file(self, rel: str, src: BinaryIO, mtime: float,
                   on_bytes: BytesFn | None = None, mode: int | None = None) -> Entry:
        name = rel.rsplit("/", 1)[-1]
        local = getattr(src, "name", None)
        if isinstance(local, str) and os.path.isabs(local) and os.path.isfile(local) \
                and abs(os.stat(local).st_mtime - mtime) < 1e-3:
            # a local file: rclone uploads it directly, keeping its time
            self._call("operations/copyfile", {
                "srcFs": os.path.dirname(local), "srcRemote": os.path.basename(local),
                "dstFs": self.root, "dstRemote": rel})
            if on_bytes:
                on_bytes(os.stat(local).st_size)
        else:
            # stage locally so rclone can upload with size and modification time known
            staging = tempfile.mkdtemp(dir=self._tmpdir)
            try:
                path = os.path.join(staging, name)
                with open(path, "wb") as f:
                    copy_stream(src, f, on_bytes)
                os.utime(path, ns=(int(mtime * 1e9), int(mtime * 1e9)))
                self._call("operations/copyfile", {
                    "srcFs": staging, "srcRemote": name, "dstFs": self.root, "dstRemote": rel})
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        entry = self.stat(rel, "skip")  # the remote may round the time
        if entry is None:
            raise OSError(f"{rel} missing after upload")
        return entry

    def set_attrs(self, rel: str, mtime: float | None, mode: int | None) -> Entry:
        # permissions do not exist on rclone remotes; no rc call sets times
        if mtime is not None:
            stamp = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            proc = subprocess.run([*self._cmd, "touch", "--no-create", "--timestamp", stamp, self._p(rel)],
                                  stdin=subprocess.DEVNULL, capture_output=True, check=False)
            if proc.returncode != 0:
                err = proc.stderr.decode(errors="replace").strip().splitlines()
                raise OSError(f"rclone touch failed: {err[-1] if err else proc.returncode}")
        entry = self.stat(rel, "skip")
        if entry is None:
            raise FileNotFoundError(rel)
        return entry

    def mkdir(self, rel: str) -> Entry:
        self._call("operations/mkdir", {"fs": self.root, "remote": rel})
        return self.stat(rel, "skip") or Entry(DIR, 0, 0.0)  # remotes without real directories

    def remove(self, rel: str, is_dir: bool) -> None:
        if is_dir:
            self._call("operations/purge", {"fs": self.root, "remote": rel})
        else:
            self._call("operations/deletefile", {"fs": self.root, "remote": rel})

    def move(self, src: str, dst: str) -> None:
        if self.stat(dst, "skip") is not None:
            raise FileExistsError(dst)
        current = self.stat(src, "skip")
        if current is None:
            raise FileNotFoundError(src)
        if current.kind == DIR:
            # server side directory move where the remote supports it
            self._call("sync/move", {"srcFs": self._p(src), "dstFs": self._p(dst),
                                     "createEmptySrcDirs": True, "deleteEmptySrcDirs": True})
            try:
                self._call("operations/rmdir", {"fs": self.root, "remote": src})
            except (FileNotFoundError, OSError):
                pass
        else:
            self._call("operations/movefile", {"srcFs": self.root, "srcRemote": src,
                                               "dstFs": self.root, "dstRemote": dst})

    def list_names(self, rel: str) -> list[str]:
        try:
            listing = self._call("operations/list", {"fs": self.root, "remote": rel,
                                                     "opt": {"noMimeType": True, "noModTime": True}})
        except FileNotFoundError:
            return []
        return [i["Name"] for i in listing.get("list") or []]

    def _info(self) -> dict:
        if self._fsinfo is None:
            try:
                self._fsinfo = self._call("operations/fsinfo", {"fs": self.root})
            except OSError:
                self._fsinfo = {}
        return self._fsinfo

    def hash_algos(self) -> set[str]:
        """Hashes the remote stores natively (e.g. md5 on Google Drive)."""
        if self._algos is None:
            native = self._info().get("Hashes") or []
            self._algos = {h.lower() for h in native} & set(HASH_ALGOS)
        return set(self._algos)

    def case_insensitive(self) -> bool | None:
        features = self._info().get("Features")
        return bool(features.get("CaseInsensitive")) if isinstance(features, dict) else None

    def free_space(self) -> int | None:
        try:
            free = self._call("operations/about", {"fs": self.root}).get("free")
        except OSError:
            return None  # not every remote can tell
        return int(free) if isinstance(free, (int, float)) else None

    def hash_many(self, rels: list[str], algo: str,
                  progress: HashProgressFn | None = None) -> dict[str, str]:
        if algo not in self.hash_algos():
            return super().hash_many(rels, algo, progress)
        result: dict[str, str] = {}
        if len(rels) > 100:
            # one listing pass is cheaper than many single lookups
            out = self._call("operations/hashsum", {"fs": self.root, "hashType": algo})
            wanted = set(rels)
            for line in out.get("hashsum") or []:
                digest, _, name = line.partition("  ")
                if name in wanted and digest:
                    result[name] = digest.lower()
            if progress:
                progress(len(rels))
            return result
        for n, rel in enumerate(rels, 1):
            try:
                result[rel] = self.hash_file(rel, algo)
            except OSError:
                pass
            if progress:
                progress(n)
        return result

    def hash_file(self, rel: str, algo: str = "sha256") -> str:
        if algo in self.hash_algos():
            out = self._call("operations/hashsumfile", {"fs": self.root, "remote": rel, "hashType": algo})
            if out.get("hash"):
                return out["hash"].lower()
        return super().hash_file(rel, algo)
