"""mysync's workbench in a pseudo-terminal, read back through pyte: for the tests of the myrcp
plugin that need the real workbench. The workbench is the myrcp wheel's launcher (mysync
depends on it, from PyPI), or `myrcp` on $PATH."""

from __future__ import annotations

import fcntl
import os
import pty
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

PRODUCT = Path(__file__).resolve().parents[1] / "src" / "mysync" / "ui"


def launcher() -> Path | None:
    try:
        from myrcp.launch import launcher as find
        return find()
    except (ImportError, FileNotFoundError):
        return None


def env_for(tmp: Path, **extra: str) -> dict[str, str]:
    """This Python for the plugin host; state, config and runtime dirs under tmp."""
    env = dict(
        os.environ,
        MYRCP_PYTHON=sys.executable,
        MYRCP_PLUGIN_PATH="",
        XDG_STATE_HOME=str(tmp / "state"),
        XDG_CONFIG_HOME=str(tmp / "config"),  # never the user's own preferences
        XDG_RUNTIME_DIR=str(tmp / "run"),  # never the user's own Vims linking in
        TERM="xterm-256color",
    )
    (tmp / "run").mkdir(exist_ok=True)
    for name in ("MYRCP_THEME", "MYRCP_EDITOR_MODE", "MYRCP_MOUSE", "MYRCP_NVIM_SERVER",
                 "MYRCP_VIM_SOCKET", "MYRCP_DIFF", "MYSYNC_PYTHON"):  # fmt: skip
        env.pop(name, None)  # the user's settings would win over the tests'
    env.update(extra)
    return env


class Pty:
    """The workbench with mysync's product, in a terminal of cols x rows."""

    def __init__(self, cwd: Path, env: dict[str, str], cols: int = 100, rows: int = 30) -> None:
        exe = launcher()
        assert exe is not None
        self.cols, self.rows = cols, rows
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.proc = subprocess.Popen([str(exe), "--product", str(PRODUCT)], stdin=slave, stdout=slave,
                                     stderr=slave, cwd=cwd, env=env, start_new_session=True)  # fmt: skip
        os.close(slave)
        self.out = bytearray()
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                self.out += data

    def type(self, data: str) -> None:
        os.write(self.master, data.encode())

    def lines(self) -> list[str]:
        import pyte

        screen = pyte.Screen(self.cols, self.rows)
        with self.lock:
            pyte.ByteStream(screen).feed(bytes(self.out))
        return screen.display

    def wait(self, pred, timeout: float = 15):
        """The screen's lines once pred(lines) holds; a string: once it is on screen."""
        if isinstance(pred, str):
            needle = pred
            pred = lambda lines: needle in "\n".join(lines)  # noqa: E731
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.lines()
            if pred(lines):
                return lines
            time.sleep(0.05)
        raise TimeoutError("not on screen:\n" + "\n".join(self.lines()))

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        os.close(self.master)
