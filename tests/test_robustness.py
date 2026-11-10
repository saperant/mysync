"""Time shifts, retries, the free space check and the post-sync command."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

import pytest

from mysync import config as config_mod
from mysync.backends import open_backend
from mysync.backends.base import parse_free_space, posix_free_space_script
from mysync.backends.local import LocalBackend
from mysync.cli import EXIT_OK, main
from mysync.model import Action, Category, SyncError
from mysync.sync import Synchronizer, synchronize

from conftest import tree, write

HOUR = 3600


# -- ignored time shifts --------------------------------------------------------

def test_time_shift_counts_as_equal(pair, past):
    p = pair(ignore_time_shift=[1])
    write(p.left / "a.txt", "same", past)
    write(p.right / "a.txt", "same", past + HOUR)
    assert p.compare().by_path["a.txt"].category == Category.EQUAL


def test_time_shift_not_ignored_by_default(pair, past):
    p = pair()
    write(p.left / "a.txt", "same", past)
    write(p.right / "a.txt", "same", past + HOUR)
    item = p.compare().by_path["a.txt"]
    assert item.category == Category.ATTRIBUTES and item.action == Action.ATTR_RL


def test_shift_after_sync_is_not_a_change(pair, past):
    p = pair(ignore_time_shift=[1, 2])
    write(p.left / "a.txt", "same", past)
    p.sync()
    os.utime(p.right / "a.txt", (past - 2 * HOUR, past - 2 * HOUR))  # e.g. a FAT disk after DST
    assert p.compare().pending() == []


def test_shift_does_not_hide_other_differences(pair, past):
    p = pair(ignore_time_shift=[1])
    write(p.left / "a.txt", "same", past)
    p.sync()
    write(p.left / "a.txt", "edit", past + HOUR + 30)
    assert p.compare().by_path["a.txt"].action == Action.COPY_LR


def test_time_shift_option_is_validated():
    for bad in ("1", [0], [25], [True]):
        with pytest.raises(config_mod.ConfigError, match="ignore_time_shift"):
            config_mod.job_from_dict({"left": "/a", "right": "/b", "ignore_time_shift": bad}, "t")
    job = config_mod.job_from_dict({"left": "/a", "right": "/b", "ignore_time_shift": 1}, "t")
    assert job.ignore_time_shift == [1.0]


# -- retries ---------------------------------------------------------------------

def _flaky_writes(monkeypatch, failures: int):
    """write_file fails the first ``failures`` times."""
    real = LocalBackend.write_file
    calls = {"n": 0}

    def write_file(self, rel, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise OSError("connection reset")
        return real(self, rel, *args, **kwargs)
    monkeypatch.setattr(LocalBackend, "write_file", write_file)
    return calls


def test_failed_items_are_retried(pair, past, monkeypatch):
    p = pair(retries=2, retry_delay=0)
    write(p.left / "a.txt", "A", past)
    write(p.left / "b.txt", "B", past)
    _flaky_writes(monkeypatch, 3)
    _, res = p.sync()
    assert res.failed == 0 and res.done == 2 and res.errors == []
    assert tree(p.right) == {"a.txt": "A", "b.txt": "B"}


def test_retries_give_up(pair, past, monkeypatch):
    p = pair(retries=1, retry_delay=0)
    write(p.left / "a.txt", "A", past)
    calls = _flaky_writes(monkeypatch, 100)
    _, res = p.sync()
    assert res.failed == 1 and calls["n"] == 2 and len(res.errors) == 1


def test_retry_failed_after_the_run(pair, past, monkeypatch):
    p = pair(retries=0)
    write(p.left / "dir/a.txt", "A", past)
    write(p.left / "b.txt", "B", past)
    _flaky_writes(monkeypatch, 1)
    with open_backend(p.job.left) as lb, open_backend(p.job.right) as rb:
        from mysync.compare import compare
        syncer = Synchronizer(compare(p.job, lb, rb), lb, rb)
        res = syncer.run()
    assert res.failed == 1
    with open_backend(p.job.left) as lb, open_backend(p.job.right) as rb:  # e.g. the TUI's button
        res = syncer.retry_failed(lb, rb)
    assert res.failed == 0 and res.done == 3 and res.state_saved
    assert p.compare().pending() == []


def test_cli_retries_option(tmp_path, past, monkeypatch, capsys):
    left, right = tmp_path / "L", tmp_path / "R"
    write(left / "a.txt", "A", past)
    right.mkdir()
    config_mod.write_template("job", str(left), str(right))
    _flaky_writes(monkeypatch, 1)
    assert main(["sync", "job", "-q", "--retries", "0"]) == 1
    assert main(["sync", "job", "-q"]) == EXIT_OK
    assert tree(right) == {"a.txt": "A"}


# -- free space ---------------------------------------------------------------------

def _free(monkeypatch, path, nbytes):
    real = LocalBackend.free_space
    monkeypatch.setattr(LocalBackend, "free_space",
                        lambda self: nbytes if self.loc.path == str(path) else real(self))


def test_not_enough_space_is_refused(pair, past, monkeypatch):
    p = pair()
    write(p.left / "big.bin", "x" * 1000, past)
    _free(monkeypatch, p.right, 999)
    cmp = p.compare()
    assert cmp.space_needed() == {"left": 0, "right": 1000}
    assert "the right side needs" in cmp.space_problem()
    with open_backend(p.job.left) as lb, open_backend(p.job.right) as rb:
        with pytest.raises(SyncError, match="not enough space"):
            synchronize(cmp, lb, rb)
        synchronize(cmp, lb, rb, force=True)
    assert (p.right / "big.bin").exists()


def test_space_freed_by_deletions_counts_without_trash(pair, past, monkeypatch):
    p = pair(deletion="delete")
    write(p.left / "old.bin", "o" * 600, past)
    write(p.left / "upd.bin", "u" * 300, past)
    p.sync()
    (p.left / "old.bin").unlink()
    write(p.left / "upd.bin", "U" * 500, past + 10)
    write(p.left / "new.bin", "n" * 500, past)
    _free(monkeypatch, p.right, 200)
    cmp = p.compare()
    assert cmp.space_needed()["right"] == 500 + 500 - 300 - 600
    assert cmp.space_problem() is None
    p.job.deletion = "trash"  # the old versions stay on the location
    assert cmp.space_needed()["right"] == 1000 and cmp.space_problem()


def test_remote_free_space_script(tmp_path):
    """The df script used by the ssh and sftp backends, run by the local shell."""
    script = posix_free_space_script(shlex.quote(str(tmp_path / "not yet/created")))
    out = subprocess.run(["sh", "-c", script], capture_output=True, check=True).stdout
    assert parse_free_space(out) == pytest.approx(shutil.disk_usage(tmp_path).free, rel=0.01)
    assert parse_free_space(b"") is None


def test_free_space_of_local_location(tmp_path):
    loc = config_mod.job_from_dict({"left": str(tmp_path / "not/yet"), "right": "/b"}, "t").left
    assert LocalBackend(loc).free_space() > 0


# -- post_sync_command ---------------------------------------------------------------

def _hook_job(tmp_path, past, command):
    left, right = tmp_path / "L", tmp_path / "R"
    write(left / "a.txt", "A", past)
    right.mkdir()
    path = config_mod.write_template("job", str(left), str(right))
    path.write_text(path.read_text().replace(
        "# post_sync_command = ", f"post_sync_command = {command!r}\n# "))
    return tmp_path / "hook.out"


def test_post_sync_command_gets_the_outcome(tmp_path, past):
    out = _hook_job(tmp_path, past, f'env | grep ^MYSYNC_ | sort > "{tmp_path}/hook.out"')
    assert main(["sync", "job", "-q"]) == EXIT_OK
    env = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert env["MYSYNC_JOB"] == "job" and env["MYSYNC_STATUS"] == "ok"
    assert env["MYSYNC_DONE"] == "1" and env["MYSYNC_BYTES"] == "1"
    assert env["MYSYNC_LOG"].endswith(".log") and env["MYSYNC_ERROR"] == ""


def test_post_sync_command_on_fatal_error(tmp_path, past):
    out = _hook_job(tmp_path, past, f'echo "$MYSYNC_STATUS: $MYSYNC_ERROR" > "{tmp_path}/hook.out"')
    assert main(["sync", "job", "-q"]) == EXIT_OK
    (tmp_path / "R").rename(tmp_path / "unmounted")
    assert main(["sync", "job", "-q"]) == 2
    assert out.read_text().startswith("failed: the right location does not exist")


def test_failing_post_sync_command_is_only_a_warning(tmp_path, past, capsys):
    _hook_job(tmp_path, past, "echo oops >&2; exit 7")
    assert main(["sync", "job", "-q"]) == EXIT_OK
    assert "post_sync_command exited with status 7: oops" in capsys.readouterr().out
