from __future__ import annotations

import os
import shutil

from mysync.model import Action, Category

from conftest import tree, write


def _trash(root):
    return [p for p in (root / ".mysync/trash").rglob("*") if p.is_file()] \
        if (root / ".mysync/trash").exists() else []


def test_rename_is_replayed_without_transfer(pair, past):
    p = pair()
    write(p.left / "report.pdf", "x" * 5000, past)
    p.sync()
    (p.left / "report.pdf").rename(p.left / "report-final.pdf")
    cmp = p.compare()
    new = cmp.by_path["report-final.pdf"]
    assert new.action == Action.MOVE_R and new.move_src == "report.pdf"
    assert cmp.by_path["report.pdf"].moved_away
    assert cmp.transfer_bytes() == 0
    assert len(cmp.pending()) == 1
    _, res = p.sync()
    assert res.bytes == 0 and res.failed == 0 and res.done == 1
    assert tree(p.right) == {"report-final.pdf": "x" * 5000}
    assert _trash(p.right) == []  # a rename is not a deletion
    assert p.compare().pending() == []


def test_move_into_new_folder_and_old_folder_removed(pair, past):
    p = pair(deletion="delete")
    write(p.left / "old/a.txt", "aaaa", past)
    write(p.left / "old/b.txt", "bbbbb", past)
    p.sync()
    os.makedirs(p.left / "new/deeper")
    (p.left / "old/a.txt").rename(p.left / "new/deeper/a.txt")
    (p.left / "old/b.txt").rename(p.left / "new/b2.txt")
    os.rmdir(p.left / "old")
    _, res = p.sync()
    assert res.bytes == 0 and res.failed == 0
    assert tree(p.right) == tree(p.left) == {
        "new": "/", "new/deeper": "/", "new/deeper/a.txt": "aaaa", "new/b2.txt": "bbbbb"}


def test_folder_rename_is_one_operation(pair, past):
    p = pair()
    for i in range(30):
        write(p.left / f"photos/2024/img{i}.jpg", f"image {i}" * 50, past + i)
    write(p.left / "photos/2024/raw/x.dng", "raw" * 100, past)
    p.sync()
    (p.left / "photos/2024").rename(p.left / "photos/2024-holiday")
    cmp = p.compare()
    folder = cmp.by_path["photos/2024-holiday"]
    assert folder.action == Action.MOVE_R and folder.move_src == "photos/2024"
    assert all(i.via_move for i in cmp.descendants(folder))
    assert [i.path for i in cmp.pending()] == ["photos/2024-holiday"]
    _, res = p.sync()
    assert res.done == 1 and res.bytes == 0 and res.failed == 0
    assert tree(p.left) == tree(p.right)
    assert p.compare().pending() == []


def test_move_on_right_side_is_replayed_on_left(pair, past):
    p = pair()
    write(p.left / "a/file.bin", "content" * 100, past)
    p.sync()
    os.makedirs(p.right / "b")
    (p.right / "a/file.bin").rename(p.right / "b/file.bin")
    cmp = p.compare()
    assert cmp.by_path["b/file.bin"].action == Action.MOVE_L
    _, res = p.sync()
    assert res.bytes == 0
    assert tree(p.left) == tree(p.right)
    assert (p.left / "b/file.bin").exists() and not (p.left / "a/file.bin").exists()


def test_moved_and_modified_is_transferred(pair, past):
    p = pair()
    write(p.left / "a.txt", "original", past)
    p.sync()
    (p.left / "a.txt").rename(p.left / "b.txt")
    write(p.left / "b.txt", "modified!", past + 100)
    cmp = p.compare()
    assert cmp.by_path["b.txt"].action == Action.COPY_LR
    assert cmp.by_path["a.txt"].action == Action.DELETE_R
    p.sync()
    assert tree(p.right) == {"b.txt": "modified!"}


def test_duplicates_prefer_same_name(pair, past):
    p = pair()
    write(p.left / "x/same.txt", "dup", past)
    write(p.left / "y/other.txt", "dup", past)
    p.sync()
    os.makedirs(p.left / "z")
    (p.left / "x/same.txt").rename(p.left / "z/same.txt")
    cmp = p.compare()
    assert cmp.by_path["z/same.txt"].move_src == "x/same.txt"


def test_mirror_detects_moves_without_state(pair, past):
    p = pair(mode="mirror", deletion="delete")
    write(p.left / "sorted/doc.odt", "doc" * 300, past)
    write(p.right / "inbox/doc.odt", "doc" * 300, past)
    cmp = p.compare()
    assert cmp.state is None
    assert cmp.by_path["sorted/doc.odt"].action == Action.MOVE_R
    _, res = p.sync()
    assert res.bytes == 0
    assert tree(p.right) == {"sorted": "/", "sorted/doc.odt": "doc" * 300}


def test_detect_moves_can_be_disabled(pair, past):
    p = pair(detect_moves=False)
    write(p.left / "a.txt", "aaa", past)
    p.sync()
    (p.left / "a.txt").rename(p.left / "b.txt")
    cmp = p.compare()
    assert cmp.by_path["b.txt"].action == Action.COPY_LR


def test_user_can_turn_move_into_copy_and_back(pair, past):
    p = pair()
    write(p.left / "a.txt", "aaa", past)
    p.sync()
    (p.left / "a.txt").rename(p.left / "b.txt")
    cmp = p.compare()
    new, old = cmp.by_path["b.txt"], cmp.by_path["a.txt"]
    # skipping the deletion of the source makes the rename impossible: copy instead
    assert cmp.set_action(old, Action.SKIP)
    assert new.action == Action.COPY_LR and not old.moved_away
    assert cmp.set_action(new, Action.MOVE_R)
    assert old.action == Action.DELETE_R and old.moved_away
    assert cmp.transfer_bytes() == 0


def test_touch_only_updates_time(pair, past):
    p = pair()
    write(p.left / "big.bin", "z" * 100000, past)
    p.sync()
    os.utime(p.left / "big.bin", (past + 500, past + 500))
    cmp = p.compare()
    item = cmp.by_path["big.bin"]
    assert item.category == Category.ATTRIBUTES and item.action == Action.ATTR_LR
    _, res = p.sync()
    assert res.bytes == 0
    assert os.stat(p.right / "big.bin").st_mtime == past + 500
    assert _trash(p.right) == []
    assert p.compare().pending() == []


def test_touch_on_both_sides_is_not_a_conflict(pair, past):
    p = pair()
    write(p.left / "f.txt", "same", past)
    p.sync()
    os.utime(p.left / "f.txt", (past + 10, past + 10))
    os.utime(p.right / "f.txt", (past + 20, past + 20))
    cmp = p.compare()
    item = cmp.by_path["f.txt"]
    assert not item.conflict and item.action == Action.ATTR_RL


def test_first_sync_identical_content_different_times(pair, past):
    p = pair()
    write(p.left / "f.txt", "same", past)
    write(p.right / "f.txt", "same", past + 1000)
    cmp = p.compare()
    assert cmp.by_path["f.txt"].action == Action.ATTR_RL


def test_verify_before_copy_disabled_transfers(pair, past):
    p = pair(verify_before_copy=False)
    write(p.left / "f.txt", "same", past)
    p.sync()
    os.utime(p.left / "f.txt", (past + 500, past + 500))
    assert p.compare().by_path["f.txt"].action == Action.COPY_LR


def test_permissions_ignored_by_default(pair, past):
    p = pair()
    write(p.left / "run.sh", "#!/bin/sh", past)
    p.sync()
    os.chmod(p.left / "run.sh", 0o755)
    assert p.compare().pending() == []


def test_permission_change_is_synced(pair, past):
    p = pair(sync_permissions=True)
    write(p.left / "run.sh", "#!/bin/sh", past)
    os.chmod(p.left / "run.sh", 0o644)
    p.sync()
    os.chmod(p.left / "run.sh", 0o750)
    cmp = p.compare()
    item = cmp.by_path["run.sh"]
    assert item.category == Category.ATTRIBUTES and item.action == Action.ATTR_LR
    _, res = p.sync()
    assert res.bytes == 0
    assert os.stat(p.right / "run.sh").st_mode & 0o777 == 0o750
    assert p.compare().pending() == []


def test_new_file_keeps_permissions_when_enabled(pair, past):
    p = pair(sync_permissions=True)
    write(p.left / "tool", "bin", past)
    os.chmod(p.left / "tool", 0o700)
    p.sync()
    assert os.stat(p.right / "tool").st_mode & 0o777 == 0o700


def test_failed_rename_keeps_source_folder(pair, past, monkeypatch):
    from mysync.backends.local import LocalBackend
    p = pair(deletion="delete")
    write(p.left / "d/a.txt", "aaaa", past)
    p.sync()
    os.makedirs(p.left / "e")
    (p.left / "d/a.txt").rename(p.left / "e/a.txt")
    write(p.left / "e/other.txt", "o", past)  # keeps it from being a folder rename
    shutil.rmtree(p.left / "d")

    def broken(self, src, dst):
        raise PermissionError("nope")
    monkeypatch.setattr(LocalBackend, "move", broken)
    _, res = p.sync()
    assert res.failed >= 1
    assert (p.right / "d/a.txt").read_text() == "aaaa"  # not lost with its folder
