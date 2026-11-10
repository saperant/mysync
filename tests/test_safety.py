from __future__ import annotations

import os
import threading

import pytest

from mysync.backends import open_backend
from mysync.compare import compare
from mysync.lock import JobLock, lock_dir
from mysync.model import Action, SyncError
from mysync.sync import Reporter, synchronize

from conftest import tree, write


def _sync_with(p, cmp, reporter=None):
    with open_backend(p.job.left) as lb, open_backend(p.job.right) as rb:
        return synchronize(cmp, lb, rb, reporter)


# -- changes between comparison and synchronization -----------------------

def test_destination_changed_after_compare_is_not_overwritten(pair, past):
    p = pair()
    write(p.left / "f.txt", "v1", past)
    p.sync()
    write(p.left / "f.txt", "left v2", past + 10)
    cmp = p.compare()
    assert cmp.by_path["f.txt"].action == Action.COPY_LR
    write(p.right / "f.txt", "right edit meanwhile", past + 20)  # someone edits the target
    res = _sync_with(p, cmp)
    assert res.changed == 1 and res.failed == 0
    assert (p.right / "f.txt").read_text() == "right edit meanwhile"
    assert cmp.by_path["f.txt"].status == "changed"
    assert p.compare().by_path["f.txt"].conflict  # seen properly next time


def test_source_changed_after_compare_is_skipped(pair, past):
    p = pair()
    write(p.left / "f.txt", "v1", past)
    cmp = p.compare()
    write(p.left / "f.txt", "v1 but longer", past + 5)
    res = _sync_with(p, cmp)
    assert res.changed == 1
    assert not (p.right / "f.txt").exists()
    p.sync()
    assert (p.right / "f.txt").read_text() == "v1 but longer"


def test_new_file_at_target_after_compare(pair, past):
    p = pair()
    write(p.left / "f.txt", "left", past)
    cmp = p.compare()
    write(p.right / "f.txt", "appeared", past)
    res = _sync_with(p, cmp)
    assert res.changed == 1 and (p.right / "f.txt").read_text() == "appeared"


def test_deletion_target_changed_after_compare(pair, past):
    p = pair(deletion="delete")
    write(p.left / "f.txt", "v1", past)
    p.sync()
    (p.left / "f.txt").unlink()
    cmp = p.compare()
    write(p.right / "f.txt", "important new content", past + 30)
    res = _sync_with(p, cmp)
    assert res.changed == 1 and (p.right / "f.txt").exists()


# -- locks -------------------------------------------------------------------

def test_lock_blocks_second_run_and_is_removed(pair, past):
    p = pair()
    write(p.left / "a.txt", "a", past)
    with JobLock(p.job, "test"):
        with pytest.raises(SyncError, match="already running"):
            p.compare()
    assert list(lock_dir().glob("*.lock")) == []
    p.sync()
    assert list(lock_dir().glob("*.lock")) == []


def test_lock_removed_after_failure(pair, past, monkeypatch):
    import mysync.sync as sync_mod
    p = pair()
    write(p.left / "a.txt", "a", past)

    def boom(*a, **k):
        raise RuntimeError("unexpected crash")
    monkeypatch.setattr(sync_mod, "finalize", boom)
    with pytest.raises(RuntimeError):
        p.sync()
    assert list(lock_dir().glob("*.lock")) == []


# -- parallel transfers ------------------------------------------------------

def test_parallel_copies(pair, past):
    p = pair(transfers=8)
    for i in range(60):
        write(p.left / f"d{i % 5}/f{i}.txt", f"content {i}", past)
    _, res = p.sync()
    assert res.failed == 0 and res.done == 65
    assert tree(p.left) == tree(p.right)
    assert p.compare().pending() == []


def test_parallel_cancel_keeps_finished_work(pair, past):
    p = pair(transfers=4)
    for i in range(40):
        write(p.left / f"f{i:02d}.bin", "x" * 50000, past)

    class StopEarly(Reporter):
        def __init__(self):
            super().__init__()
            self.n = 0
            self.lock = threading.Lock()

        def item_done(self, item):
            with self.lock:
                self.n += 1
                if self.n == 5:
                    self.cancel_event.set()
    res = _sync_with(p, p.compare(), StopEarly())
    assert res.cancelled and res.state_saved
    copied = len(list(p.right.glob("*.bin")))
    assert 5 <= copied < 40
    assert not list(p.right.glob(".*mysync-tmp"))
    assert len(p.compare().pending()) == 40 - copied  # finished ones are recorded


# -- renames with other changes ----------------------------------------------

def test_rename_and_touch_is_rename_plus_time(pair, past):
    p = pair()
    write(p.left / "a.bin", "z" * 10000, past)
    p.sync()
    (p.left / "a.bin").rename(p.left / "b.bin")
    os.utime(p.left / "b.bin", (past + 999, past + 999))
    cmp = p.compare()
    item = cmp.by_path["b.bin"]
    assert item.action == Action.MOVE_R and item.move_attrs == "dst"
    _, res = p.sync()
    assert res.bytes == 0 and res.failed == 0
    assert os.stat(p.right / "b.bin").st_mtime == past + 999
    assert p.compare().pending() == []


def test_rename_and_chmod(pair, past):
    p = pair(sync_permissions=True)
    write(p.left / "s.sh", "#!/bin/sh\necho hi\n", past)
    os.chmod(p.left / "s.sh", 0o644)
    p.sync()
    (p.left / "s.sh").rename(p.left / "run.sh")
    os.chmod(p.left / "run.sh", 0o755)
    _, res = p.sync()
    assert res.bytes == 0
    assert os.stat(p.right / "run.sh").st_mode & 0o777 == 0o755


def test_renamed_on_one_side_touched_on_other(pair, past):
    p = pair()
    write(p.left / "doc.txt", "same content" * 100, past)
    p.sync()
    (p.left / "doc.txt").rename(p.left / "doc-renamed.txt")
    os.utime(p.right / "doc.txt", (past + 500, past + 500))  # touched, not edited
    cmp = p.compare()
    new = cmp.by_path["doc-renamed.txt"]
    assert new.action == Action.MOVE_R and new.move_attrs == "src"  # right's time is newer
    assert not cmp.by_path["doc.txt"].conflict
    _, res = p.sync()
    assert res.bytes == 0 and res.failed == 0
    assert tree(p.left) == tree(p.right) == {"doc-renamed.txt": "same content" * 100}
    assert os.stat(p.left / "doc-renamed.txt").st_mtime == past + 500
    assert p.compare().pending() == []


def test_renamed_on_one_side_edited_on_other_stays_conflict(pair, past):
    p = pair()
    write(p.left / "doc.txt", "original", past)
    p.sync()
    (p.left / "doc.txt").rename(p.left / "doc2.txt")
    write(p.right / "doc.txt", "edited!!", past + 500)
    cmp = p.compare()
    assert cmp.by_path["doc.txt"].conflict
    assert cmp.by_path["doc2.txt"].action == Action.COPY_LR


def test_empty_files_are_never_renamed_or_attribute_updated(pair, past):
    p = pair()
    write(p.left / "empty1", "", past)
    p.sync()
    (p.left / "empty1").rename(p.left / "empty2")
    cmp = p.compare()
    assert cmp.by_path["empty2"].action == Action.COPY_LR
    assert cmp.by_path["empty1"].action == Action.DELETE_R
    p.sync()
    os.utime(p.left / "empty2", (past + 100, past + 100))
    assert p.compare().by_path["empty2"].action == Action.COPY_LR


# -- symbolic links ----------------------------------------------------------

def test_links_mode_copies_relative_inside_links(pair, past, tmp_path):
    p = pair(symlinks="links")
    write(p.left / "docs/real.txt", "r", past)
    os.symlink("docs/real.txt", p.left / "shortcut")
    os.symlink("../outside", p.left / "escapes")
    os.symlink("/etc/hostname", p.left / "absolute")
    cmp = p.compare()
    assert set(cmp.by_path) == {"docs", "docs/real.txt", "shortcut"}
    assert len(cmp.warnings) == 2
    _, res = p.sync()
    assert res.failed == 0
    assert os.readlink(p.right / "shortcut") == "docs/real.txt"
    assert not os.path.lexists(p.right / "escapes")
    # retarget the link on the right: propagated to the left
    write(p.right / "docs/other.txt", "o", past)
    os.unlink(p.right / "shortcut")
    os.symlink("docs/other.txt", p.right / "shortcut")
    p.sync()
    assert os.readlink(p.left / "shortcut") == "docs/other.txt"
    assert p.compare().pending() == []


def test_links_skipped_by_default(pair, past):
    p = pair()
    write(p.left / "real.txt", "r", past)
    os.symlink("real.txt", p.left / "link")
    p.sync()
    assert tree(p.right) == {"real.txt": "r"}
