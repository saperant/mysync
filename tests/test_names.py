"""Names that differ only in case or Unicode form.

The test file systems are case-sensitive, so case-insensitive storage is
simulated with ``case_sensitive = false`` (or a backend that says so)."""

from __future__ import annotations

import os
import unicodedata

from mysync.backends.local import LocalBackend
from mysync.model import Action

from conftest import tree, write

NFC = unicodedata.normalize("NFC", "café.txt")
NFD = unicodedata.normalize("NFD", "café.txt")


def names(root) -> list[str]:
    return sorted(n for n in os.listdir(root) if n != ".mysync")


def test_different_case_is_the_same_file(pair, past):
    p = pair(case_sensitive=False)
    write(p.left / "Foo.txt", "same", past)
    write(p.right / "foo.txt", "same", past)
    cmp = p.compare()
    assert [i.path for i in cmp.items] == ["Foo.txt"]
    assert cmp.pending() == []  # no state: the spelling is left as it is
    assert "named 'Foo.txt' on the left, 'foo.txt' on the right" in cmp.items[0].reason
    p.sync()
    assert names(p.left) == ["Foo.txt"] and names(p.right) == ["foo.txt"]
    assert p.compare().pending() == []


def test_new_file_goes_into_the_folder_as_spelled_there(pair, past):
    p = pair(case_sensitive=False)
    write(p.left / "Docs/Report.txt", "r", past)
    (p.right / "docs").mkdir()
    cmp, res = p.sync()
    assert res.failed == 0
    assert tree(p.right) == {"docs": "/", "docs/Report.txt": "r"}
    assert p.compare().pending() == []


def test_case_only_rename_is_propagated(pair, past):
    p = pair(case_sensitive=False)
    write(p.left / "dir/a.txt", "A", past)
    write(p.left / "dir/sub/b.txt", "B", past)
    p.sync()
    os.rename(p.left / "dir", p.left / "Dir")
    os.rename(p.left / "Dir/a.txt", p.left / "Dir/A.txt")
    cmp = p.compare()
    assert {i.path: i.name_fix for i in cmp.pending()} == {"Dir": "right", "Dir/A.txt": "right"}
    assert cmp.counts()["names"] == 2
    _, res = p.sync()
    assert res.failed == 0 and res.done == 2
    assert tree(p.right) == {"Dir": "/", "Dir/A.txt": "A", "Dir/sub": "/", "Dir/sub/b.txt": "B"}
    assert p.compare().pending() == []


def test_case_only_rename_on_the_right(pair, past):
    p = pair(case_sensitive=False)
    write(p.left / "readme.md", "x", past)
    p.sync()
    os.rename(p.right / "readme.md", p.right / "README.md")
    assert p.compare().by_path["readme.md"].name_fix == "left"
    p.sync()
    assert names(p.left) == ["README.md"]
    assert p.compare().pending() == []


def test_renamed_and_edited(pair, past):
    p = pair(case_sensitive=False)
    write(p.left / "notes.txt", "v1", past)
    p.sync()
    os.rename(p.left / "notes.txt", p.left / "Notes.txt")
    write(p.left / "Notes.txt", "v2 longer", past + 10)
    item = p.compare().by_path["Notes.txt"]
    assert item.action == Action.COPY_LR and item.name_fix == "right"
    p.sync()
    assert tree(p.right) == {"Notes.txt": "v2 longer"}
    assert p.compare().pending() == []


def test_mirror_gives_the_right_the_left_spelling(pair, past):
    p = pair(case_sensitive=False, mode="mirror")
    write(p.left / "Photo.JPG", "img", past)
    write(p.right / "photo.jpg", "img", past)
    assert p.compare().by_path["Photo.JPG"].name_fix == "right"
    p.sync()
    assert names(p.right) == ["Photo.JPG"]


def test_skipping_leaves_the_name_alone(pair, past):
    p = pair(case_sensitive=False)
    write(p.left / "a.txt", "A", past)
    p.sync()
    os.rename(p.left / "a.txt", p.left / "A.txt")
    _, res = p.sync(adjust=lambda cmp: cmp.set_action(cmp.by_path["A.txt"], Action.SKIP))
    assert names(p.right) == ["a.txt"]
    # the rename is still seen next time
    assert p.compare().by_path["A.txt"].name_fix == "right"


def test_failed_rename_is_seen_again(pair, past, monkeypatch):
    p = pair(case_sensitive=False)
    write(p.left / "a.txt", "A", past)
    p.sync()
    os.rename(p.left / "a.txt", p.left / "A.txt")

    def broken(self, src, dst):
        raise PermissionError("read-only")
    with monkeypatch.context() as m:
        m.setattr(LocalBackend, "move", broken)
        _, res = p.sync()
    assert res.failed == 1
    assert p.compare().by_path["A.txt"].name_fix == "right"
    p.sync()
    assert names(p.right) == ["A.txt"]


def test_colliding_names_are_left_alone(pair, past):
    p = pair(case_sensitive=False)
    write(p.left / "a.txt", "small", past)
    write(p.left / "A.txt", "capital", past)
    write(p.left / "b.txt", "b", past)
    write(p.right / "A.txt", "other", past)
    cmp, res = p.sync()
    assert any("differ only in case" in w and "A.txt, a.txt" in w for w in cmp.warnings)
    assert tree(p.right) == {"A.txt": "other", "b.txt": "b"}
    assert tree(p.left) == {"A.txt": "capital", "a.txt": "small", "b.txt": "b"}


def test_case_sensitive_keeps_both(pair, past):
    p = pair(case_sensitive=True)
    write(p.left / "a.txt", "1", past)
    write(p.left / "A.txt", "2", past)
    p.sync()
    assert tree(p.right) == {"A.txt": "2", "a.txt": "1"}


def test_detected_case_insensitive_side(pair, past, monkeypatch):
    right = str(pair().right)
    monkeypatch.setattr(LocalBackend, "case_insensitive",
                        lambda self: True if self.loc.path == right else None)
    p = pair()  # case_sensitive = "auto"
    write(p.left / "Foo.txt", "same", past)
    write(p.right / "foo.txt", "same", past)
    assert [i.path for i in p.compare().items] == ["Foo.txt"]


def test_auto_on_case_sensitive_storage(pair, past):
    p = pair()
    write(p.left / "Foo.txt", "1", past)
    write(p.right / "foo.txt", "2", past)
    assert [i.path for i in p.compare().items] == ["Foo.txt", "foo.txt"]


def test_unicode_forms_are_the_same_name(pair, past):
    p = pair()
    write(p.left / NFD, "x", past)
    write(p.right / NFC, "x", past)
    cmp = p.compare()
    assert len(cmp.items) == 1 and cmp.pending() == []
    p.sync()
    write(p.left / NFD, "changed", past + 10)
    p.sync()
    assert names(p.right) == [NFC]  # updated in place
    assert (p.right / NFC).read_text() == "changed"


def test_unicode_normalization_off(pair, past):
    p = pair(unicode_normalization=False)
    write(p.left / NFD, "x", past)
    write(p.right / NFC, "x", past)
    assert len(p.compare().items) == 2


def test_existing_state_still_works(pair, past):
    p = pair(case_sensitive=True, unicode_normalization=False)  # matched exactly
    write(p.left / "Foo.txt", "1", past)
    p.sync()
    (p.left / "Foo.txt").unlink()
    p.job.case_sensitive, p.job.unicode_normalization = False, True
    assert p.compare().by_path["Foo.txt"].action == Action.DELETE_R
