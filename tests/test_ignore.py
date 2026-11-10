"""Leaving items out of one round (Space in the TUI)."""

from __future__ import annotations

import os

from mysync.model import Action

from conftest import tree, write


def test_ignored_folder_is_left_alone_and_seen_again(pair, past):
    p = pair(deletion="delete")
    write(p.left / "work/a.txt", "a1", past)
    write(p.left / "other.txt", "o1", past)
    p.sync()
    write(p.left / "work/a.txt", "a2", past + 100)
    write(p.left / "work/new.txt", "n", past)
    write(p.left / "other.txt", "o2", past + 100)

    def ignore_work(cmp):
        changed, failed = cmp.set_ignored([cmp.by_path["work"]], True)
        assert (changed, failed) == (3, 0)
        assert all(cmp.by_path[x].action == Action.SKIP for x in ("work", "work/a.txt", "work/new.txt"))
        assert cmp.counts()["ignored"] == 3
        assert [i.path for i in cmp.pending()] == ["other.txt"]

    _, res = p.sync(adjust=ignore_work)
    assert res.failed == 0 and res.state_saved
    assert tree(p.right) == {"work": "/", "work/a.txt": "a1", "other.txt": "o2"}
    # the old baseline was kept: the next comparison sees the changes again
    cmp = p.compare()
    assert cmp.by_path["work/a.txt"].action == Action.COPY_LR
    assert cmp.by_path["work/new.txt"].action == Action.COPY_LR
    assert not any(i.ignored for i in cmp.items)


def test_ignored_deletion_keeps_folder(pair, past):
    p = pair(deletion="delete")
    write(p.left / "d/x.txt", "x", past)
    write(p.left / "d/y.txt", "y", past)
    p.sync()
    (p.left / "d/x.txt").unlink()
    (p.left / "d/y.txt").unlink()
    os.rmdir(p.left / "d")
    cmp = p.compare()
    assert cmp.by_path["d"].action == Action.DELETE_R
    cmp.set_ignored([cmp.by_path["d/x.txt"]], True)
    assert cmp.by_path["d"].action == Action.NONE  # still holds x.txt: left alone
    cmp.set_ignored([cmp.by_path["d/x.txt"]], False)
    assert cmp.by_path["d"].action == Action.DELETE_R and not cmp.by_path["d"].held
    p.sync(adjust=lambda c: c.set_ignored([c.by_path["d/x.txt"]], True))
    assert tree(p.right) == {"d": "/", "d/x.txt": "x"}
    assert tree(p.left) == {}
    assert p.compare().by_path["d/x.txt"].action == Action.DELETE_R


def test_take_back_restores_choice(pair, past):
    p = pair()
    write(p.left / "a.txt", "A", past)
    cmp = p.compare()
    item = cmp.by_path["a.txt"]
    cmp.set_action(item, Action.DELETE_L)
    cmp.set_ignored([item], True)
    assert item.ignored and item.action == Action.SKIP
    assert not cmp.set_action(item, Action.COPY_LR)  # no choices while ignored
    cmp.set_ignored([item], False)
    assert not item.ignored and item.action == Action.DELETE_L


def test_ignoring_a_rename_ignores_both_ends(pair, past):
    p = pair()
    write(p.left / "old.bin", "x" * 100, past)
    p.sync()
    (p.left / "old.bin").rename(p.left / "new.bin")
    cmp = p.compare()
    new, old = cmp.by_path["new.bin"], cmp.by_path["old.bin"]
    assert new.action == Action.MOVE_R
    cmp.set_ignored([new], True)
    assert new.ignored and old.ignored and cmp.pending() == []
    cmp.set_ignored([new], False)
    assert new.action == Action.MOVE_R and old.action == Action.DELETE_R and old.moved_away


def test_folder_rename_content_cannot_be_ignored_alone(pair, past):
    p = pair()
    write(p.left / "a/f.txt", "f", past)
    p.sync()
    (p.left / "a").rename(p.left / "b")
    cmp = p.compare()
    assert cmp.by_path["b"].action == Action.MOVE_R
    assert cmp.set_ignored([cmp.by_path["b/f.txt"]], True) == (0, 1)
    changed, failed = cmp.set_ignored([cmp.by_path["b"]], True)
    assert failed == 0 and cmp.pending() == []
    assert all(i.ignored for i in cmp.items)
