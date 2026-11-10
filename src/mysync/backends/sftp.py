"""SFTP backend (paramiko).  Only an SFTP server is needed on the remote side.

Honours ``~/.ssh/config`` (HostName, User, Port, IdentityFile, ProxyCommand),
the SSH agent and the default key files.  Authentication must not require
interactive input; use keys or an agent.
"""

from __future__ import annotations

import errno
import os
import posixpath
import shlex
import stat
import threading
from pathlib import Path
from typing import BinaryIO, Iterable

import paramiko

from mysync.backends.base import (
    Backend, Blocks, BytesFn, ExcludeFn, HashProgressFn, ScanProgressFn, copy_stream, parent,
    parse_block_hashes, parse_free_space, parse_hash_output, patch_stream, posix_block_hash_script,
    posix_free_space_script, posix_hash_probe, posix_hash_script, posix_patch_script, tmp_name,
)
from mysync.model import DIR, FILE, LINK, Entry, SyncError


def _entry(attr, target: str | None = None) -> Entry:
    mode = attr.st_mode or 0
    mtime = float(attr.st_mtime or 0)
    if stat.S_ISDIR(mode):
        return Entry(DIR, 0, mtime)
    if stat.S_ISLNK(mode):
        return Entry(LINK, 0, mtime, target=target)
    if stat.S_ISREG(mode):
        return Entry(FILE, attr.st_size or 0, mtime, stat.S_IMODE(mode))
    return Entry("?", 0, mtime)


def _missing(e: OSError) -> bool:
    return isinstance(e, FileNotFoundError) or getattr(e, "errno", None) == errno.ENOENT


class SftpBackend(Backend):
    time_resolution = 1.0  # SFTP v3 transfers whole seconds

    def __init__(self, loc):
        super().__init__(loc)
        self.client: paramiko.SSHClient | None = None
        self._tls = threading.local()
        self._sessions: list[paramiko.SFTPClient] = []
        self._sessions_lock = threading.Lock()
        self.root = loc.path
        self._algos: set[str] = set()
        self._delta = False
        self._gnu_split = True

    # -- connection -------------------------------------------------------
    def connect(self) -> None:
        opts = self.loc.options
        host_cfg: dict = {}
        ssh_config = Path("~/.ssh/config").expanduser()
        if ssh_config.is_file():
            host_cfg = paramiko.SSHConfig.from_path(str(ssh_config)).lookup(self.loc.host)
        hostname = host_cfg.get("hostname", self.loc.host)
        port = self.loc.port or int(host_cfg.get("port", 22))
        user = self.loc.user or host_cfg.get("user") or None
        key_files = [os.path.expanduser(opts["identity_file"])] if opts.get("identity_file") else [
            os.path.expanduser(k) for k in host_cfg.get("identityfile", [])
        ]

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        known_hosts = Path(opts.get("known_hosts", "~/.ssh/known_hosts")).expanduser()
        if known_hosts.is_file():
            client.load_host_keys(str(known_hosts))
        policy = opts.get("host_key_policy", "strict")
        if policy == "accept-new":
            known_hosts.parent.mkdir(parents=True, exist_ok=True)
            known_hosts.touch(exist_ok=True)
            client.load_host_keys(str(known_hosts))  # new keys are saved here
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        elif policy == "strict":
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            raise SyncError(f"{self.label}: host_key_policy must be 'strict' or 'accept-new'")

        sock = None
        if host_cfg.get("proxycommand"):
            sock = paramiko.ProxyCommand(host_cfg["proxycommand"])
        try:
            client.connect(
                hostname, port=port, username=user,
                key_filename=[k for k in key_files if os.path.exists(k)] or None,
                allow_agent=True, look_for_keys=True, sock=sock,
                timeout=float(opts.get("connect_timeout", 15)),
            )
        except paramiko.SSHException as e:
            client.close()
            hint = " (unknown host key: connect once with ssh or set host_key_policy = \"accept-new\")" \
                if "not found in known_hosts" in str(e) else ""
            raise SyncError(f"{self.label}: {e}{hint}") from e
        except OSError as e:
            client.close()
            raise SyncError(f"{self.label}: cannot connect to {hostname}:{port}: {e}") from e
        self.client = client
        if self.root.startswith("~"):
            home = self.sftp.normalize(".")
            self.root = posixpath.join(home, self.root[1:].lstrip("/"))
        self.root = self.root.rstrip("/") or "/"
        self._algos = self._probe_hashes()

    @property
    def sftp(self) -> paramiko.SFTPClient:
        """One SFTP session per thread, all over the same SSH connection."""
        session = getattr(self._tls, "session", None)
        if session is None:
            session = paramiko.SFTPClient.from_transport(self.client.get_transport())
            self._tls.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _exec(self, command: str, stdin: bytes | Iterable[bytes] = b"") -> tuple[int, bytes, str]:
        """Run a shell command over the same SSH connection."""
        chan = self.client.get_transport().open_session()
        try:
            chan.exec_command("sh -c " + shlex.quote(command))
            failure: list[BaseException] = []

            def feed() -> None:
                try:
                    for chunk in ([stdin] if isinstance(stdin, bytes) else stdin):
                        if chunk:
                            chan.sendall(chunk)
                except BaseException as e:  # noqa: BLE001 - re-raised in the caller's thread
                    failure.append(e)
                finally:
                    chan.shutdown_write()
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
            out = bytearray()
            while data := chan.recv(65536):
                out += data
            writer.join()
            err = b""
            while chan.recv_stderr_ready():
                err += chan.recv_stderr(65536)
            rc = chan.recv_exit_status()
            if failure:
                raise failure[0]
            return rc, bytes(out), err.decode(errors="replace").strip()
        finally:
            chan.close()

    def _probe_hashes(self) -> set[str]:
        """Checksums can be computed on the server if it allows shell commands."""
        if self.loc.options.get("remote_hash", True) is False:
            return set()
        try:
            rc, out, _ = self._exec(posix_hash_probe())
        except (paramiko.SSHException, OSError):
            return set()
        names = set(out.decode(errors="replace").split())
        self._delta = rc == 0 and "delta" in names and "sha256" in names
        self._gnu_split = "gnu-split" in names
        if rc != 0 or "no-xargs" in names:
            return set()
        return names & {"sha256", "sha1", "md5"}

    def hash_algos(self) -> set[str]:
        return set(self._algos)

    def hash_many(self, rels: list[str], algo: str,
                  progress: HashProgressFn | None = None) -> dict[str, str]:
        if algo not in self._algos:
            return super().hash_many(rels, algo, progress)
        result: dict[str, str] = {}
        for start in range(0, len(rels), 2000):
            batch = rels[start:start + 2000]
            _, out, _ = self._exec(posix_hash_script(shlex.quote(self.root), algo),
                                "\0".join(batch).encode(errors="surrogateescape") + b"\0")
            result.update(parse_hash_output(out, batch))
            if progress:
                progress(start + len(batch))
        return result

    def hash_file(self, rel: str, algo: str = "sha256") -> str:
        if algo in self._algos:
            found = self.hash_many([rel], algo)
            if rel in found:
                return found[rel]
        return super().hash_file(rel, algo)

    def free_space(self) -> int | None:
        if self.loc.options.get("remote_hash", True) is False:
            return None  # no shell commands wanted on the server
        try:
            rc, out, _ = self._exec(posix_free_space_script(shlex.quote(self.root)))
        except (paramiko.SSHException, OSError):
            return None
        return parse_free_space(out) if rc == 0 else None

    def ensure_connected(self) -> None:
        transport = self.client.get_transport() if self.client else None
        if transport is not None and transport.is_active():
            return
        self.close()
        self._tls = threading.local()
        self.root = self.loc.path
        self.connect()

    def close(self) -> None:
        with self._sessions_lock:
            for session in self._sessions:
                session.close()
            self._sessions.clear()
        if self.client:
            self.client.close()
            self.client = None

    def _abs(self, rel: str) -> str:
        return posixpath.join(self.root, rel) if rel else self.root

    # -- operations -------------------------------------------------------
    def root_exists(self) -> bool:
        try:
            return stat.S_ISDIR(self.sftp.stat(self.root).st_mode)
        except OSError:
            return False

    def _lookup(self, path: str, attr, symlinks: str) -> Entry | None:
        entry = _entry(attr)
        if entry.kind != LINK:
            return entry
        if symlinks == "skip":
            return None
        if symlinks == "links":
            return _entry(attr, self.sftp.readlink(path))
        try:
            return _entry(self.sftp.stat(path))
        except OSError:
            return None  # dangling link

    def stat(self, rel: str, symlinks: str) -> Entry | None:
        path = self._abs(rel)
        try:
            return self._lookup(path, self.sftp.lstat(path), symlinks)
        except OSError as e:
            if _missing(e):
                return None
            raise

    def scan(self, excluded: ExcludeFn, symlinks: str,
             progress: ScanProgressFn | None = None) -> dict[str, Entry]:
        result: dict[str, Entry] = {}
        stack = [""]
        visited: set[str] = set()
        while stack:
            rel_dir = stack.pop()
            try:
                attrs = self.sftp.listdir_attr(self._abs(rel_dir))
            except OSError as e:
                if _missing(e):
                    continue
                raise
            for attr in attrs:
                rel = f"{rel_dir}/{attr.filename}" if rel_dir else attr.filename
                entry = self._lookup(self._abs(rel), attr, symlinks)
                if entry is None or entry.kind not in (FILE, DIR, LINK):
                    continue
                if excluded(rel, entry.kind == DIR):
                    continue
                result[rel] = entry
                if entry.kind == DIR:
                    if symlinks == "follow":
                        real = self.sftp.normalize(self._abs(rel))
                        if real in visited:
                            continue
                        visited.add(real)
                    stack.append(rel)
                if progress and len(result) % 500 == 0:
                    progress(len(result))
        if progress:
            progress(len(result))
        return result

    def open_read(self, rel: str) -> BinaryIO:
        try:
            f = self.sftp.open(self._abs(rel), "rb")
        except OSError as e:
            if _missing(e):
                raise FileNotFoundError(rel) from e
            raise
        f.prefetch()
        return f

    def write_file(self, rel: str, src: BinaryIO, mtime: float,
                   on_bytes: BytesFn | None = None, mode: int | None = None) -> Entry:
        target = self._abs(rel)
        tmp = self._abs(tmp_name(rel))
        try:
            with self.sftp.open(tmp, "wb") as dst:
                dst.set_pipelined(True)
                copy_stream(src, dst, on_bytes)
            if mode is not None:
                self.sftp.chmod(tmp, mode)
            self.sftp.utime(tmp, (mtime, mtime))
            try:
                self.sftp.posix_rename(tmp, target)
            except OSError:
                # server without the posix-rename extension
                try:
                    self.sftp.remove(target)
                except OSError:
                    pass
                self.sftp.rename(tmp, target)
        except BaseException:
            try:
                self.sftp.remove(tmp)
            except OSError:
                pass
            raise
        return _entry(self.sftp.stat(target))

    def write_link(self, rel: str, target: str) -> Entry:
        tmp = self._abs(tmp_name(rel))
        self.sftp.symlink(target, tmp)
        try:
            self.sftp.posix_rename(tmp, self._abs(rel))
        except BaseException:
            try:
                self.sftp.remove(tmp)
            except OSError:
                pass
            raise
        return self.stat(rel, "links")

    def set_attrs(self, rel: str, mtime: float | None, mode: int | None) -> Entry:
        path = self._abs(rel)
        if mode is not None:
            self.sftp.chmod(path, mode)
        if mtime is not None:
            self.sftp.utime(path, (mtime, mtime))
        return _entry(self.sftp.stat(path))

    def mkdir(self, rel: str) -> Entry:
        path = self._abs(rel)
        try:
            return _entry(self.sftp.stat(path))
        except OSError as e:
            if not _missing(e):
                raise
        if rel:
            self.mkdir(parent(rel))
        self.sftp.mkdir(path)
        return _entry(self.sftp.stat(path))

    def remove(self, rel: str, is_dir: bool) -> None:
        path = self._abs(rel)
        if not is_dir or stat.S_ISLNK(self.sftp.lstat(path).st_mode):
            self.sftp.remove(path)
            return
        for attr in self.sftp.listdir_attr(path):
            child = f"{rel}/{attr.filename}"
            self.remove(child, stat.S_ISDIR(attr.st_mode or 0))
        self.sftp.rmdir(path)

    def move(self, src: str, dst: str) -> None:
        if parent(dst):
            self.mkdir(parent(dst))
        try:
            self.sftp.lstat(self._abs(dst))
        except OSError as e:
            if not _missing(e):
                raise
        else:
            raise FileExistsError(dst)
        self.sftp.rename(self._abs(src), self._abs(dst))

    def list_names(self, rel: str) -> list[str]:
        try:
            return self.sftp.listdir(self._abs(rel))
        except OSError as e:
            if _missing(e):
                return []
            raise

    # -- delta transfer (needs shell access and GNU coreutils) ---------------
    def supports_delta(self) -> bool:
        return self._delta

    def block_hashes(self, rel: str, bs: int) -> list[str]:
        rc, out, err = self._exec(posix_block_hash_script(shlex.quote(self._abs(rel)), bs, self._gnu_split))
        if rc != 0:
            raise OSError(err or f"block checksums of {rel} failed")
        return parse_block_hashes(out)

    def patch_file(self, rel: str, blocks: Blocks, size: int, mtime: float, mode: int | None,
                   backup: str | None, expect: tuple[int, str], on_bytes: BytesFn | None) -> Entry:
        stat_script = 'echo ok'
        script = posix_patch_script(
            shlex.quote(self._abs(rel)), shlex.quote(self._abs(tmp_name(rel))), size, mtime, mode,
            shlex.quote(self._abs(backup)) if backup else None, expect[0], expect[1], stat_script,
            self._gnu_split)
        rc, _, err = self._exec(script, patch_stream(blocks, on_bytes))
        if rc != 0:
            raise OSError(f"patching {rel} failed: {err or rc}")
        return _entry(self.sftp.stat(self._abs(rel)))
