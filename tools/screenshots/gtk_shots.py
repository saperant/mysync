"""Screenshots of mysync in the GTK workbench (run inside sandbox.sh): argv[1] out dir,
then steps: "key:<xdotool keys>", "type:<text>", "sleep:<s>", "shot:<name>"."""
import os, subprocess, sys, time
from pathlib import Path

OUT = Path(sys.argv[1]); W, H = 1500, 900
(Path.home() / ".local/state/mysync/workbench.state").unlink(missing_ok=True)
n = next(n for n in range(90, 130) if not Path(f"/tmp/.X11-unix/X{n}").exists())
xvfb = subprocess.Popen(["Xvfb", f":{n}", "-screen", "0", f"{W}x{H}x24", "-nolisten", "tcp"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not Path(f"/tmp/.X11-unix/X{n}").exists():
    time.sleep(0.05)
env = dict(os.environ, DISPLAY=f":{n}", GDK_BACKEND="x11", NO_AT_BRIDGE="1", GSK_RENDERER="cairo",
           MYRCP_PLUGIN_PATH="", PATH=os.environ.get("MYRCP_GTK_DIR", "") + ":" + os.environ["PATH"])
env.pop("WAYLAND_DISPLAY", None)
xd = lambda *a: subprocess.run(["xdotool", *a], env={"DISPLAY": f":{n}"}, check=False, timeout=10)
log = open(OUT / "gtk.log", "w")
proc = subprocess.Popen(["mysync", "ui", "--gtk"], env=env, stdout=log, stderr=subprocess.STDOUT)
try:
    time.sleep(4)
    xd("search", "--sync", "--onlyvisible", "--class", "", "windowsize", "%@", str(W), str(H), "windowmove", "%@", "0", "0")
    time.sleep(1)
    for step in sys.argv[2:]:
        kind, _, arg = step.partition(":")
        if kind == "key": xd("key", "--delay", "120", *arg.split())
        elif kind == "type": xd("type", "--delay", "40", arg)
        elif kind in ("click", "dclick"):  # click:x,y
            x, y = arg.split(",")
            xd("mousemove", x, y); time.sleep(0.2)
            xd("click", *(["--repeat", "2", "--delay", "80"] if kind == "dclick" else []), "1")
        elif kind == "sleep": time.sleep(float(arg))
        elif kind == "shot":
            time.sleep(1)
            subprocess.run(["import", "-display", f":{n}", "-window", "root", str(OUT / f"{arg}.png")], check=True)
        time.sleep(0.4)
finally:
    proc.kill(); xvfb.terminate()
