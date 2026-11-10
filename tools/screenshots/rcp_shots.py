"""Screenshots of mysync in the terminal workbench (run inside sandbox.sh): argv[1] the out dir."""
import fcntl, os, pty, struct, subprocess, sys, termios, threading, time
from pathlib import Path
import pyte
sys.path.insert(0, os.path.dirname(__file__))
from term_svg import render

(Path.home() / ".local/state/mysync/workbench.state").unlink(missing_ok=True)
OUT = Path(sys.argv[1]); COLS, ROWS = 160, 38
env = dict(os.environ, TERM="xterm-256color", MYRCP_PLUGIN_PATH="")
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
proc = subprocess.Popen(["mysync"], stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
os.close(slave)
buf = bytearray(); lock = threading.Lock()
def reader():
    while True:
        try: d = os.read(master, 65536)
        except OSError: return
        if not d: return
        with lock: buf.extend(d)
threading.Thread(target=reader, daemon=True).start()
def screen():
    s = pyte.Screen(COLS, ROWS)
    with lock: pyte.ByteStream(s).feed(bytes(buf))
    return s
def wait(needle, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if needle in "\n".join(screen().display): return
        time.sleep(0.1)
    raise TimeoutError(needle + "\n" + "\n".join(screen().display))
def key(k, pause=0.4): os.write(master, k.encode()); time.sleep(pause)
def shot(name, title):
    time.sleep(0.8); (OUT / name).write_text(render(screen(), title))
    (OUT / (name[:-4] + ".txt")).write_text("\n".join(screen().display))
try:
    wait("Last sync")
    key("\x10"); wait("Commands"); key("perspective mysync", 0.6); key("\r", 1.0); wait("Properties")
    key("\x1b[B", 0.3); key("\x1b[B", 0.6)  # photos: its job file at the side
    shot("rcp-jobs.svg", "mysync — the myrcp workbench")
    key("\x1b[A", 0.3); key("\x1b[A", 0.3)
    key("\r"); wait("todo.txt")
    key("\x1b[B", 0.3); key("\x1b[B", 0.6)  # onto garden.txt: its properties at the side
    shot("rcp-plan.svg", "mysync — documents")
    key("\x1b1", 0.8); wait("Last sync")
    for _ in range(3): key("\x1b[B", 0.2)
    key("\r"); wait("boat-trailer", 30)
    shot("rcp-selective-plan.svg", "mysync — projects")
    key("t"); wait("2026/", 30); key("*", 1.5)
    shot("rcp-selection.svg", "mysync — projects: selection")
finally:
    proc.kill()
