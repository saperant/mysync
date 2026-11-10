"""Talks to myrcp's plugin host (`python -m myrcp.host`) as the workbench would, for
testing mysync's myrcp plugin (src/mysync/ui/plugins/mysync) without a
terminal.

The myrcp SDK comes from $MYRCP_SDK, else the installed myrcp package (the `ui` and `test`
extras), else a myrcp checkout beside this one (../myrcp/python).
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGINS_DIR = REPO / "src" / "mysync" / "ui" / "plugins"


def find_sdk() -> Path | None:
    candidates = []
    if os.environ.get("MYRCP_SDK"):
        candidates.append(Path(os.environ["MYRCP_SDK"]))
    try:
        import myrcp

        candidates.append(Path(myrcp.__file__).resolve().parents[1])
    except ImportError:
        pass
    candidates.append(REPO.parent / "myrcp" / "python")
    return next((c for c in candidates if (c / "myrcp" / "host.py").exists()), None)


SDK = find_sdk()


class Peer:
    def __init__(self, cwd: Path, python: str = sys.executable, env: dict[str, str] | None = None) -> None:
        assert SDK is not None
        sys.path.insert(0, str(SDK))
        from myrcp._msgpack import Unpacker, packb

        self._packb = packb
        full = dict(os.environ, PYTHONPATH=str(SDK), MYRCP_PLUGIN_PATH=str(PLUGINS_DIR), **(env or {}))
        self.proc = subprocess.Popen([python, "-m", "myrcp.host"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=cwd, env=full)  # fmt: skip
        self.unpacker = Unpacker()
        self.inbox: list = []
        self.next_id = 1

    def send(self, msg) -> None:
        self.proc.stdin.write(self._packb(msg))
        self.proc.stdin.flush()

    def notify(self, method, params) -> None:
        self.send([2, method, [params]])

    def request(self, method, params):
        msgid = self.next_id
        self.next_id += 1
        self.send([0, msgid, method, [params]])
        msg = self.wait(lambda m: m[0] == 1 and m[1] == msgid)
        assert msg[2] is None, msg[2]
        return msg[3]

    def wait(self, pred, timeout=5.0):
        deadline = time.monotonic() + timeout
        while True:
            for i, m in enumerate(self.inbox):
                if pred(m):
                    return self.inbox.pop(i)
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"no matching message; got {self.inbox}")
            fd = self.proc.stdout.fileno()
            r, _, _ = select.select([fd], [], [], left)
            if r:
                data = os.read(fd, 65536)
                if not data:
                    raise EOFError(self.proc.stderr.read().decode())
                self.unpacker.feed(data)
                self.inbox.extend(self.unpacker)

    def wait_notify(self, method, pred=lambda p: True, timeout=5.0):
        return self.wait(lambda m: m[0] == 2 and m[1] == method and pred(m[2][0]), timeout)[2][0]

    def close(self) -> str:
        if self.proc.poll() is None:
            self.notify("shutdown", {})
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        return self.proc.stderr.read().decode()
