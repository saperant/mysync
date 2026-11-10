"""mysync's myrcp plugin (src/mysync/ui/plugins/mysync) over myrcp's plugin protocol:
two local folders, compare, sync, the selection, conflicts as markers; and in the real
workbench (a pty), the diff program and a job edited in a tab. Skipped without myrcp
(pip install -e '.[test]')."""

import os
import re
import sys
import time
from pathlib import Path

import pytest
from myrcp_peer import SDK, Peer
from myrcp_pty import Pty, env_for, launcher

# This venv has mysync; the plugin host runs in it, with the SDK on its path.
MYSYNC_PYTHON = sys.executable
pytestmark = pytest.mark.skipif(SDK is None, reason="no myrcp SDK (pip install -e '.[test]', or MYRCP_SDK)")
try:
    import pyte  # noqa: F401
except ImportError:
    pyte = None
need_workbench = pytest.mark.skipif(launcher() is None or pyte is None,
                                    reason="no myrcp launcher (the myrcp wheel) or no pyte")  # fmt: skip


def find(tree, pred):
    if pred(tree):
        return tree
    for c in tree.get("children", []) + [tree[k] for k in ("child", "dialog") if k in tree]:
        if (hit := find(c, pred)) is not None:
            return hit
    return None


def table(tree, id):
    return find(tree, lambda n: n.get("id") == id and n.get("t") == "table")


@pytest.fixture
def setup(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "a.txt").write_text("from the left\n")
    (left / "sub").mkdir()
    (left / "sub" / "b.txt").write_text("b\n")
    (right / "c.txt").write_text("from the right\n")
    conf = tmp_path / "conf"
    (conf / "conf.d").mkdir(parents=True)
    (conf / "conf.d" / "t.toml").write_text(f'left = "{left}"\nright = "{right}"\n')
    env = {
        "MYSYNC_CONFIG_DIR": str(conf),
        "MYSYNC_LOG_DIR": str(tmp_path / "logs"),
        "XDG_RUNTIME_DIR": str(tmp_path / "run"),
        "MYSYNC_PYTHON": MYSYNC_PYTHON,
    }
    (tmp_path / "run").mkdir()
    return tmp_path, left, right, env


def test_the_plugin_runs_in_this_python(setup):
    """No MYSYNC_PYTHON: the plugin host started with mysync's Python loads it itself."""
    tmp, _, _, env = setup
    env = dict(env)
    env.pop("MYSYNC_PYTHON")
    p = Peer(tmp, env=env)
    try:
        res = p.request("initialize", {"protocol": 1})
        assert "mysync" in [pl["id"] for pl in res["plugins"]]
        assert not res.get("delegates"), res["delegates"]
    finally:
        p.close()


def test_compare_and_sync(setup):
    tmp, _left, right, env = setup
    p = Peer(tmp, python=MYSYNC_PYTHON, env=dict(env, MYRCP_HOST_PLUGINS="mysync"))
    try:
        res = p.request("initialize", {"protocol": 1})
        assert [pl["id"] for pl in res["plugins"]] == ["mysync"]

        jobs = p.request("view.open", {"view": "mysync.jobs"})["tree"]
        assert [r["key"] for r in table(jobs, "jobs")["rows"]] == ["t"]
        p.notify("view.event", {"view": "mysync.jobs", "widget": "jobs", "kind": "activate", "value": "t"})
        assert p.wait_notify("view.show") == {"view": "mysync.job#t"}

        opened = p.request("view.open", {"view": "mysync.job#t"})
        assert opened["title"] == "⇄ t"
        begin = p.wait_notify("job.begin")
        assert begin["title"] == "Compare t"
        end = p.wait_notify("job.end", lambda e: e["job"] == begin["job"], timeout=20)
        assert end["ok"], end
        update = p.wait_notify("view.update", lambda u: (table(u["tree"], "plan") or {}).get("total", 0) > 0)
        plan = table(update["tree"], "plan")
        actions = {r["key"]: r["cells"][3]["text"].strip() for r in plan["rows"]}
        assert actions == {"a.txt": "──▶", "sub": "──▶", "c.txt": "◀──"}
        # A tree of folders: sub is new on the left, so it starts closed (its content goes with it).
        assert plan["tree"] == 2
        assert [(r["key"], r["depth"], r.get("expanded")) for r in plan["rows"]] == [
            ("sub", 0, False),
            ("a.txt", 0, None),
            ("c.txt", 0, None),
        ]
        assert plan["rows"][0]["cells"][2]["text"] == "sub/"
        p.notify("view.event", {"view": "mysync.job#t", "widget": "plan", "kind": "expand", "value": "sub"})
        update = p.wait_notify("view.update", lambda u: table(u["tree"], "plan")["total"] == 4)
        rows = table(update["tree"], "plan")["rows"]
        assert [(r["key"], r["depth"]) for r in rows[:2]] == [("sub", 0), ("sub/b.txt", 1)]
        assert rows[1]["cells"][2]["text"] == "b.txt"
        # ← on a file goes to its folder; on an open folder closes it.
        p.notify("view.event", {"view": "mysync.job#t", "widget": "plan", "kind": "collapse", "value": "sub/b.txt"})
        update = p.wait_notify("view.update", lambda u: "cursor" in table(u["tree"], "plan"))
        assert table(update["tree"], "plan")["cursor"][0] == 0
        p.notify("view.event", {"view": "mysync.job#t", "widget": "plan", "kind": "collapse", "value": "sub"})
        p.wait_notify("view.update", lambda u: table(u["tree"], "plan")["total"] == 3)

        # Mark c.txt and make the left like the right instead: c.txt is deleted on the right.
        p.notify("command.run", {"command": "mysync.job.mark", "view": "mysync.job#t", "widget": "plan",
                                 "key": "c.txt", "index": 2})  # fmt: skip
        p.notify("command.run", {"command": "mysync.job.right", "view": "mysync.job#t", "widget": "plan",
                                 "key": "a.txt", "index": 1})  # fmt: skip
        update = p.wait_notify("view.update", lambda u: "▶✖" in str(table(u["tree"], "plan")))
        assert "1 marked" in str(update["tree"])

        p.notify("command.run", {"command": "mysync.job.sync", "view": "mysync.job#t"})
        update = p.wait_notify("view.update", lambda u: u["tree"]["t"] == "modal")
        assert update["tree"]["title"] == "Start synchronization?"
        p.notify("view.event", {"view": "mysync.job#t", "widget": "ok", "kind": "press", "value": {"form": {}}})
        begin = p.wait_notify("job.begin", lambda b: b["title"] == "Sync t")
        end = p.wait_notify("job.end", lambda e: e["job"] == begin["job"], timeout=30)
        assert end["ok"], end
        done = p.wait_notify("view.update", lambda u: "Finished:" in str(u["tree"]))
        assert "Close" in str(done["tree"]["dialog"])

        assert (right / "a.txt").read_text() == "from the left\n"
        assert (right / "sub" / "b.txt").exists()
        assert not (right / "c.txt").exists()  # deleted, into the trash
        assert list((right / ".mysync" / "trash").rglob("c.txt"))

        # Close: a clean run compares again and finds everything equal. Only the finished
        # comparison writes a summary with counts ("Comparing…" shows an empty plan too).
        p.inbox.clear()
        p.notify("view.event", {"view": "mysync.job#t", "widget": "close", "kind": "press", "value": {"form": {}}})
        begin = p.wait_notify("job.begin", lambda b: b["title"] == "Compare t")
        p.wait_notify("job.end", lambda e: e["job"] == begin["job"], timeout=20)

        def compared(u):
            plan = table(u["tree"], "plan")
            return plan is not None and plan["total"] == 0 and "conflicts 0" in str(u["tree"]["children"][4])

        p.wait_notify("view.update", compared, timeout=20)
    finally:
        log = p.close()
    assert "Traceback" not in log, log


def test_a_closed_folder_stands_for_its_content(setup):
    tmp, left, right, env = setup
    for side in (left, right):
        (side / "d").mkdir()
        (side / "d" / "same.txt").write_text("same")
    (left / "d" / "new.txt").write_text("new")
    p = Peer(tmp, python=MYSYNC_PYTHON, env=dict(env, MYRCP_HOST_PLUGINS="mysync"))
    try:
        p.request("initialize", {"protocol": 1})
        p.request("view.open", {"view": "mysync.job#t"})
        update = p.wait_notify("view.update", lambda u: "d/new.txt" in str(table(u["tree"], "plan")), timeout=20)
        rows = table(update["tree"], "plan")["rows"]
        assert ("d", 0, True) in [(r["key"], r["depth"], r.get("expanded")) for r in rows]  # unchanged: open
        p.notify("view.event", {"view": "mysync.job#t", "widget": "plan", "kind": "collapse", "value": "d"})
        p.wait_notify("view.update", lambda u: "d/new.txt" not in str(table(u["tree"], "plan")))
        # Make the left like the right on the closed folder: what is new in it goes.
        p.notify("command.run", {"command": "mysync.job.left", "view": "mysync.job#t", "widget": "plan", "key": "d",
                                 "index": 0})  # fmt: skip
        p.notify("view.event", {"view": "mysync.job#t", "widget": "plan", "kind": "expand", "value": "d"})
        update = p.wait_notify("view.update", lambda u: "d/new.txt" in str(table(u["tree"], "plan")))
        new = next(r for r in table(update["tree"], "plan")["rows"] if r["key"] == "d/new.txt")
        assert new["cells"][3]["text"].strip() == "✖◀"
        # The flat list again: whole paths, no depth.
        p.notify("command.run", {"command": "mysync.job.tree", "view": "mysync.job#t"})
        update = p.wait_notify("view.update", lambda u: "tree" not in table(u["tree"], "plan"))
        flat = table(update["tree"], "plan")
        assert "d/new.txt" in [r["cells"][2]["text"] for r in flat["rows"]]
        assert all("depth" not in r for r in flat["rows"])
    finally:
        log = p.close()
    assert "Traceback" not in log, log


def test_compare_the_two_copies(setup):
    """= on a file: both copies to the workbench's compare (a missing one as an empty file)."""
    tmp, left, right, env = setup
    (right / "a.txt").write_text("from the right\n")
    p = Peer(tmp, python=MYSYNC_PYTHON, env=dict(env, MYRCP_HOST_PLUGINS="mysync"))
    try:
        p.request("initialize", {"protocol": 1})
        p.request("view.open", {"view": "mysync.job#t"})
        p.wait_notify("view.update", lambda u: "c.txt" in str(table(u["tree"], "plan")), timeout=20)
        run = {"command": "mysync.job.diff", "view": "mysync.job#t", "widget": "plan"}
        p.notify("command.run", dict(run, key="a.txt", index=1))
        both = p.wait_notify("compare.open")
        assert both == {"left": str(left / "a.txt"), "right": str(right / "a.txt"), "title": "⇆ a.txt"}
        p.notify("command.run", dict(run, key="c.txt", index=2))  # only on the right
        one = p.wait_notify("compare.open", lambda c: c["title"] == "⇆ c.txt")
        assert one["right"] == str(right / "c.txt")
        assert Path(one["left"]).name == "c.txt" and Path(one["left"]).read_bytes() == b""
    finally:
        log = p.close()
    assert "Traceback" not in log, log


@need_workbench
def test_compare_runs_the_diff_program(setup):
    """The workbench runs workbench.diff (vimdiff by default) with the two files."""
    tmp, left, right, env = setup
    env.pop("MYSYNC_PYTHON")
    (right / "a.txt").write_text("from the right\n")
    got = tmp / "diffed"
    fake = tmp / "fakediff"
    fake.write_text(f'#!/bin/sh\necho "$@" > {got}\n')
    fake.chmod(0o755)
    e = env_for(tmp, **dict(env, MYRCP_DIFF=str(fake)))
    t = Pty(tmp, e, cols=160, rows=40)
    try:
        top = t.wait(lambda ls: "Sync jobs" in ls[0])[0]
        t.type("\x1b" + re.search(r"(\d+) Sync jobs", top).group(1))  # Alt+N
        t.wait(lambda ls: any(" t " in f" {line} " for line in ls))
        t.type("\r")
        t.wait(lambda ls: "c.txt" in "\n".join(ls), timeout=20)
        t.type("\x1b[B")  # sub/, a.txt
        t.type("=")
        for _ in range(100):
            if got.exists():
                break
            time.sleep(0.1)
        assert got.read_text().split() == [str(left / "a.txt"), str(right / "a.txt")]
        t.type("\x11")
        assert t.proc.wait(timeout=10) == 0
    finally:
        t.close()


def test_new_job_form(setup):
    tmp, left, right, env = setup
    p = Peer(tmp, python=MYSYNC_PYTHON, env=dict(env, MYRCP_HOST_PLUGINS="mysync"))
    try:
        p.request("initialize", {"protocol": 1})
        p.request("view.open", {"view": "mysync.jobs"})
        p.notify("command.run", {"command": "mysync.jobs.new", "view": "mysync.jobs"})
        p.wait_notify("view.update", lambda u: u["tree"]["t"] == "modal")
        p.notify("view.event", {"view": "mysync.jobs", "widget": "create", "kind": "press",
                                "value": {"form": {"name": "", "left": "", "right": ""}}})  # fmt: skip
        p.wait_notify("view.update", lambda u: "name, left and right are required" in str(u["tree"]))
        form = {"name": "fresh", "left": str(left), "right": str(right)}
        p.notify("view.event", {"view": "mysync.jobs", "widget": "create", "kind": "press", "value": {"form": form}})
        update = p.wait_notify("view.update", lambda u: u["tree"]["t"] != "modal")
        assert [r["key"] for r in table(update["tree"], "jobs")["rows"]] == ["fresh", "t"]
    finally:
        p.close()
    assert sys.executable


def test_selection_browser(setup):
    tmp, left, right, env = setup
    (left / "a.txt").unlink()
    (left / "sub" / "b.txt").unlink()
    (left / "sub").rmdir()
    (right / "archive" / "deep").mkdir(parents=True)
    (right / "archive" / "old.bin").write_bytes(b"x" * 100)
    (right / "archive" / "deep" / "d.txt").write_text("d")
    (right / "keep").mkdir()
    (right / "keep" / "k.txt").write_text("k")
    job = tmp / "conf" / "conf.d" / "t.toml"
    job.write_text(job.read_text() + 'selective = "left"\n')
    p = Peer(tmp, python=MYSYNC_PYTHON, env=dict(env, MYRCP_HOST_PLUGINS="mysync"))
    try:
        p.request("initialize", {"protocol": 1})
        opened = p.request("view.open", {"view": "mysync.select#t"})
        assert opened["title"] == "☑ t"
        scan = p.wait_notify("job.begin", lambda b: b["title"] == "Scan t")
        p.wait_notify("job.end", lambda e: e["job"] == scan["job"], timeout=20)
        update = p.wait_notify(
            "view.update", lambda u: (find(u["tree"], lambda n: n.get("t") == "tree") or {}).get("total", 0) > 1
        )
        tree = find(update["tree"], lambda n: n.get("t") == "tree")
        rows = {r["key"]: r for r in tree["rows"]}
        assert list(rows) == ["", "archive", "keep", "c.txt"]  # folders first; only the root is open
        assert rows["archive"]["expandable"] and not rows["archive"]["expanded"]
        assert {r["check"] for r in rows.values()} == {"all"}

        def send(kind, key):
            p.notify("view.event", {"view": "mysync.select#t", "widget": "tree", "kind": kind, "value": key})

        send("toggle", "archive")
        update = p.wait_notify("view.update", lambda u: "Not saved." in str(u["tree"]))
        rows = {r["key"]: r for r in find(update["tree"], lambda n: n.get("t") == "tree")["rows"]}
        assert rows["archive"]["check"] == "none" and rows[""]["check"] == "some"

        send("expand", "archive")
        update = p.wait_notify("view.update", lambda u: "archive/deep" in str(u["tree"]))
        keys = [r["key"] for r in find(update["tree"], lambda n: n.get("t") == "tree")["rows"]]
        assert keys[:5] == ["", "archive", "archive/deep", "archive/old.bin", "keep"]

        send("collapse", "archive/deep")  # closed already: the cursor goes to its parent
        update = p.wait_notify("view.update", lambda u: "cursor" in str(u["tree"]))
        assert find(update["tree"], lambda n: n.get("t") == "tree")["cursor"][0] == 1

        p.notify("command.run", {"command": "mysync.select.save", "view": "mysync.select#t"})
        status = p.wait_notify("status", lambda s: s["text"].startswith("Saved"))
        saved = Path(status["text"].removeprefix("Saved ").split(";")[0])
        assert saved.exists() and "archive" in saved.read_text()
        assert p.wait_notify("view.show") == {"view": "mysync.job#t"}
        assert p.wait_notify("view.close") == {"view": "mysync.select#t"}
    finally:
        log = p.close()
    assert "Traceback" not in log, log


def test_conflicts_become_markers(setup):
    tmp, left, right, env = setup
    (left / "both.txt").write_text("left")
    (right / "both.txt").write_text("right!")
    p = Peer(tmp, python=MYSYNC_PYTHON, env=dict(env, MYRCP_HOST_PLUGINS="mysync"))
    try:
        p.request("initialize", {"protocol": 1})
        p.request("view.open", {"view": "mysync.job#t"})
        m = p.wait_notify("markers.set", lambda m: m["markers"], timeout=20)
        assert m["set"] == "conflicts:t"
        (marker,) = m["markers"]
        assert marker["view"] == "mysync.job#t" and marker["path"] == str(left / "both.txt")
        assert marker["severity"] == "warning" and "both.txt" in marker["message"]
        p.notify("view.close", {"view": "mysync.job#t"})
        assert p.wait_notify("markers.set", lambda m: not m["markers"])["set"] == "conflicts:t"
    finally:
        p.close()


@need_workbench
def test_mysync_reloads_a_job_edited_in_a_tab(tmp_path):
    left, right = tmp_path / "l", tmp_path / "r"
    left.mkdir()
    right.mkdir()
    conf = tmp_path / "mysync" / "conf.d"
    conf.mkdir(parents=True)
    (conf / "t.toml").write_text(f'left = "{left}"\nright = "{right}"\n')
    # An "editor" that changes the job and exits: the tab closes, and only then may
    # mysync reload the file (the request is answered when the editor is done).
    editor = tmp_path / "ed"
    editor.write_text('#!/bin/sh\nsleep 0.5\nprintf \'mode = "mirror"\\n\' >> "$1"\n')
    editor.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    env = env_for(tmp_path, MYRCP_EDITOR_MODE="tab", VISUAL=str(editor), MYSYNC_LOG_DIR=str(tmp_path / "logs"),
                  MYSYNC_CONFIG_DIR=str(tmp_path / "mysync"))  # fmt: skip
    t = Pty(work, env)
    screen = t
    try:
        screen.wait("Sync jobs")
        # By name, not Alt+N: tabs of hosts that come up later may still shift the numbers.
        t.type("\x10")
        screen.wait("Commands")
        t.type("open sync jobs")
        time.sleep(0.3)
        t.type("\r")
        screen.wait("Last sync")
        time.sleep(0.3)
        t.type("\r")
        screen.wait("two-way, compare by")
        t.type("e")
        screen.wait("mirror, compare by", timeout=20)
        t.type("\x11")
        assert t.proc.wait(timeout=10) == 0
    finally:
        t.close()


