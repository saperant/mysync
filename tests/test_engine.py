from __future__ import annotations

import shutil

from mysync.model import Action, Category

from conftest import tree, write


def test_first_sync_merges_both_sides(pair, past):
    p = pair()
    write(p.left / "a.txt", "A", past)
    write(p.left / "dir/b.txt", "B", past)
    write(p.right / "c.txt", "C", past)
    cmp, res = p.sync()
    assert res.failed == 0 and res.state_saved
    assert tree(p.left) == tree(p.right) == {"a.txt": "A", "c.txt": "C", "dir": "/", "dir/b.txt": "B"}
    # second run: nothing to do
    assert p.compare().pending() == []


def test_deletion_is_propagated_not_resurrected(pair, past):
    p = pair(deletion="delete")
    write(p.left / "keep.txt", "k", past)
    write(p.left / "gone.txt", "g", past)
    p.sync()
    (p.right / "gone.txt").unlink()  # delete on one side
    cmp = p.compare()
    item = cmp.by_path["gone.txt"]
    assert item.action == Action.DELETE_L
    assert item.reason == "deleted on right"
    p.sync()
    assert tree(p.left) == tree(p.right) == {"keep.txt": "k"}


def test_deleted_file_goes_to_trash(pair, past):
    p = pair()  # deletion = "trash" by default
    write(p.left / "x/doc.txt", "v1", past)
    p.sync()
    (p.left / "x/doc.txt").unlink()
    p.sync()
    assert not (p.right / "x/doc.txt").exists()
    trashed = list((p.right / ".mysync/trash").rglob("doc.txt"))
    assert len(trashed) == 1 and trashed[0].read_text() == "v1"


def test_overwritten_version_kept_in_trash(pair, past):
    p = pair()
    write(p.left / "f.txt", "old", past)
    p.sync()
    write(p.left / "f.txt", "new", past + 100)
    p.sync()
    assert (p.right / "f.txt").read_text() == "new"
    assert [t.read_text() for t in (p.right / ".mysync/trash").rglob("f.txt")] == ["old"]


def test_modification_propagates_both_directions(pair, past):
    p = pair()
    write(p.left / "l.txt", "1", past)
    write(p.left / "r.txt", "1", past)
    p.sync()
    write(p.left / "l.txt", "left edit", past + 10)
    write(p.right / "r.txt", "right edit", past + 10)
    p.sync()
    assert tree(p.left) == tree(p.right) == {"l.txt": "left edit", "r.txt": "right edit"}


def test_both_modified_is_conflict_and_skipped(pair, past):
    p = pair()
    write(p.left / "f.txt", "base", past)
    p.sync()
    write(p.left / "f.txt", "left!", past + 10)
    write(p.right / "f.txt", "right!!", past + 20)
    cmp, res = p.sync()
    item = cmp.by_path["f.txt"]
    assert item.conflict and item.action == Action.SKIP
    assert (p.left / "f.txt").read_text() == "left!"
    assert (p.right / "f.txt").read_text() == "right!!"
    # still a conflict next time (baseline kept)
    assert p.compare().by_path["f.txt"].conflict


def test_conflict_policy_newer(pair, past):
    p = pair(conflict="newer")
    write(p.left / "f.txt", "base", past)
    p.sync()
    write(p.left / "f.txt", "left!", past + 10)
    write(p.right / "f.txt", "right!!", past + 20)
    p.sync()
    assert (p.left / "f.txt").read_text() == "right!!"


def test_delete_vs_modify_conflict(pair, past):
    p = pair(conflict="newer")
    write(p.left / "f.txt", "base", past)
    p.sync()
    (p.left / "f.txt").unlink()
    write(p.right / "f.txt", "modified", past + 10)
    p.sync()  # newer policy keeps the modified file
    assert tree(p.left) == tree(p.right) == {"f.txt": "modified"}


def test_deleted_folder_with_new_content_on_other_side_is_kept(pair, past):
    p = pair(deletion="delete")
    write(p.left / "d/old.txt", "o", past)
    p.sync()
    import shutil
    shutil.rmtree(p.left / "d")
    write(p.right / "d/new.txt", "n", past + 10)
    cmp, res = p.sync()
    assert res.failed == 0
    assert tree(p.left) == tree(p.right) == {"d": "/", "d/new.txt": "n"}


def test_deleted_folder_removed_as_a_whole(pair, past):
    p = pair(deletion="delete")
    for i in range(5):
        write(p.left / f"d/sub/{i}.txt", str(i), past)
    p.sync()
    import shutil
    shutil.rmtree(p.right / "d")
    cmp = p.compare()
    assert cmp.by_path["d"].action == Action.DELETE_L
    assert all(cmp.by_path[f"d/sub/{i}.txt"].covered for i in range(5))
    cmp, res = p.sync()
    assert res.failed == 0
    assert tree(p.left) == tree(p.right) == {}
    assert p.compare().items == []


def test_mirror_mode(pair, past):
    p = pair(mode="mirror", deletion="delete")
    write(p.left / "a.txt", "A", past)
    write(p.right / "extra.txt", "E", past)
    write(p.right / "a.txt", "changed", past + 50)
    p.sync()
    assert tree(p.right) == {"a.txt": "A"}


def test_update_mode_respects_deletions_on_target(pair, past):
    p = pair(mode="update")
    write(p.left / "a.txt", "A", past)
    write(p.right / "only-right.txt", "R", past)
    p.sync()
    assert tree(p.right) == {"a.txt": "A", "only-right.txt": "R"}
    (p.right / "a.txt").unlink()
    cmp, _ = p.sync()
    assert not (p.right / "a.txt").exists()  # not copied again
    write(p.left / "a.txt", "A2", past + 100)
    p.sync()
    assert (p.right / "a.txt").read_text() == "A2"


def test_excludes(pair, past):
    p = pair(exclude=["*.tmp", "cache/", "/top-only.txt"])
    write(p.left / "a.tmp", "x", past)
    write(p.left / "cache/x.bin", "x", past)
    write(p.left / "sub/top-only.txt", "kept", past)
    write(p.left / "top-only.txt", "skipped", past)
    p.sync()
    assert tree(p.right) == {"sub": "/", "sub/top-only.txt": "kept"}


def test_content_compare(pair, past):
    p = pair(compare="content")
    write(p.left / "same.txt", "abc", past)
    write(p.right / "same.txt", "abc", past + 500)
    write(p.left / "diff.txt", "abc", past)
    write(p.right / "diff.txt", "abd", past)
    cmp = p.compare()
    assert cmp.by_path["same.txt"].category == Category.EQUAL
    assert cmp.by_path["diff.txt"].category == Category.DIFFERENT


def test_safety_limit_blocks_mass_deletion(pair, past):
    import pytest
    from mysync.model import SyncError
    p = pair(deletion="delete")
    for i in range(20):
        write(p.left / f"{i}.txt", "x", past)
    p.sync()
    for i in range(20):
        (p.left / f"{i}.txt").unlink()
    with pytest.raises(SyncError, match="max_delete_percent"):
        p.sync()
    assert len(list(p.right.iterdir())) == 20
    p.sync(force=True)
    assert tree(p.right) == {}


def test_missing_root_after_previous_sync_aborts(pair, past):
    import pytest
    import shutil
    from mysync.model import SyncError
    p = pair()
    write(p.left / "a.txt", "A", past)
    p.sync()
    shutil.rmtree(p.right)  # e.g. unmounted drive
    with pytest.raises(SyncError, match="does not exist"):
        p.compare()


def test_lost_state_is_safe(pair, past, tmp_path):
    p = pair(deletion="delete")
    write(p.left / "a.txt", "A", past)
    write(p.left / "b.txt", "B", past)
    p.sync()
    for f in (tmp_path / "config/db").iterdir():
        f.unlink()
    (p.left / "a.txt").unlink()
    p.sync()  # without state a deletion cannot be told apart from a new file
    assert tree(p.left) == tree(p.right) == {"a.txt": "A", "b.txt": "B"}


def test_state_stored_inside_location(pair, past, tmp_path):
    p = pair(left={"url": str(tmp_path / "left"), "state_dir": ".mysync"},
             right={"url": str(tmp_path / "right"), "state_dir": str(tmp_path / "rstate")})
    write(p.left / "a.txt", "A", past)
    p.sync()
    assert list((p.left / ".mysync").glob("*-left.json.gz"))
    assert list((tmp_path / "rstate").glob("*-right.json.gz"))
    (p.left / "a.txt").unlink()
    cmp = p.compare()
    assert cmp.by_path["a.txt"].action == Action.DELETE_R


def test_user_override_and_failed_item_retried(pair, past):
    p = pair()
    write(p.left / "a.txt", "A", past)
    write(p.left / "b.txt", "B", past)

    def skip_b(cmp):
        cmp.set_action(cmp.by_path["b.txt"], Action.SKIP)
    p.sync(adjust=skip_b)
    assert tree(p.right) == {"a.txt": "A"}
    assert p.compare().by_path["b.txt"].action == Action.COPY_LR


def test_deleted_folder_with_similarly_named_sibling(pair, past):
    # "a-b" sorts between "a" and "a/..."; the folder's content must still be found
    p = pair(deletion="delete")
    write(p.left / "a/x.txt", "x", past)
    write(p.left / "a-b/y.txt", "y", past)
    p.sync()
    shutil.rmtree(p.left / "a")
    cmp = p.compare()
    assert cmp.by_path["a/x.txt"].covered
    _, res = p.sync()
    assert res.failed == 0 and res.done == 1
    assert tree(p.right) == {"a-b": "/", "a-b/y.txt": "y"}


def test_trash_outside_synced_tree(pair, past, tmp_path):
    # e.g. keep old versions out of a public web root
    p = pair(left={"url": str(tmp_path / "left")},
             right={"url": str(tmp_path / "right"), "trash_dir": "../right-trash"})
    write(p.left / "page.php", "v1", past)
    p.sync()
    write(p.left / "page.php", "v2", past + 10)
    p.sync()
    assert tree(p.right) == {"page.php": "v2"}
    assert [t.read_text() for t in (tmp_path / "right-trash").rglob("page.php")] == ["v1"]


def test_quick_same_size_edit_after_sync_is_detected(pair):
    import time
    p = pair()
    now = time.time()
    write(p.left / "f.txt", "aaaa", now)
    p.sync()
    write(p.left / "f.txt", "bbbb", now + 0.5)  # same size, within time_tolerance
    p.sync()
    assert (p.right / "f.txt").read_text() == "bbbb"


def test_two_runs_in_the_same_second_keep_both_old_versions(pair, past):
    p = pair()
    write(p.left / "f.txt", "v1", past)
    p.sync()
    write(p.left / "f.txt", "v2", past + 10)
    p.sync()
    write(p.left / "f.txt", "v3", past + 20)
    _, res = p.sync()
    assert res.failed == 0
    assert sorted(t.read_text() for t in (p.right / ".mysync/trash").rglob("f.txt")) == ["v1", "v2"]


def test_old_trash_is_purged(pair, past):
    p = pair(trash_keep_days=1)
    old = p.right / ".mysync/trash/2020-01-01_000000.000000/x.txt"
    write(old, "ancient", past)
    write(p.right / ".mysync/trash/2020-01-01_000000/legacy.txt", "ancient", past)
    write(p.left / "a.txt", "a", past)
    p.sync()
    assert list((p.right / ".mysync/trash").iterdir()) == []
