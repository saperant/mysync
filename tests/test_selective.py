from __future__ import annotations

import os

from mysync import selection
from mysync.model import Action
from mysync.selection import ALL, NONE, SOME, Selection

from conftest import tree, write


def _select(p, **rules):
    sel = selection.load(p.job)
    for path, value in rules.items():
        sel.set(path.replace("__", "/"), value)
    selection.save(p.job, sel)


def _trash_files(root):
    return sorted(x.relative_to(root).as_posix() for x in (root / ".mysync/trash").rglob("*") if x.is_file())


# -- the selection rules -----------------------------------------------------

def test_selection_rules():
    sel = Selection()
    assert sel.is_everything and sel.state("a") == ALL
    sel.set("Projects", False)
    sel.set("Projects/2026", True)
    assert not sel.is_selected("Projects/2025/x")
    assert sel.is_selected("Projects/2026/x") and sel.is_selected("Other")
    assert sel.state("Projects") == SOME and sel.state("Projects/2025") == NONE
    assert sel.state("") == SOME
    assert sel.wanted("Projects", True) and not sel.wanted("Projects/2025", True)
    sel.toggle("Projects")  # partly selected -> everything
    assert sel.rules == {} and sel.is_everything
    sel.toggle("")  # nothing at all
    assert sel.state("x/y") == NONE and not sel.default
    sel.set("x", True)
    assert sel.rules == {"": False, "x": True}
    sel.set("x", False)  # same as inherited: rule disappears
    assert sel.rules == {"": False}


def test_selection_file_roundtrip(pair):
    p = pair(selective="left")
    sel = Selection({"": False, "Työ/ä \"q\"": True, "Työ/ä \"q\"/old": False})
    path = selection.save(p.job, sel)
    assert path.name == "test.toml"
    assert selection.load(p.job).rules == sel.rules


# -- syncing ---------------------------------------------------------------

def test_unselected_folder_is_not_downloaded(pair, past):
    p = pair(selective="left")
    write(p.right / "Archive/big.bin", "archive", past)
    write(p.right / "Work/a.txt", "a", past)
    _select(p, Archive=False)
    cmp, res = p.sync()
    assert res.failed == 0
    assert tree(p.left) == {"Work": "/", "Work/a.txt": "a"}
    assert p.compare().pending() == []
    # selecting it again brings it back
    _select(p, Archive=True)
    p.sync()
    assert (p.left / "Archive/big.bin").read_text() == "archive"


def test_deselecting_frees_local_copy(pair, past):
    p = pair(selective="left")
    write(p.left / "Archive/2020/a.bin", "a" * 1000, past)
    write(p.left / "Archive/b.bin", "b" * 500, past)
    write(p.left / "Work/w.txt", "w", past)
    p.sync()
    _select(p, Archive=False)
    cmp = p.compare()
    assert cmp.by_path["Archive/b.bin"].action == Action.EVICT_L
    assert cmp.by_path["Archive"].action == Action.EVICT_L
    assert cmp.freed_bytes() == 1500
    assert cmp.safety_problem() is None
    _, res = p.sync()
    assert res.failed == 0 and res.freed == 1500
    assert tree(p.left) == {"Work": "/", "Work/w.txt": "w"}
    assert (p.right / "Archive/2020/a.bin").exists() and (p.right / "Archive/b.bin").exists()
    assert not (p.left / ".mysync/trash").exists()  # freed, not trashed
    assert p.compare().pending() == []


def test_local_changes_are_uploaded_before_freeing(pair, past):
    p = pair(selective="left")
    write(p.left / "Old/edit.txt", "v1", past)
    p.sync()
    write(p.left / "Old/edit.txt", "v2 edited", past + 100)
    write(p.left / "Old/new.txt", "created locally", past)
    _select(p, Old=False)
    cmp = p.compare()
    assert cmp.by_path["Old/edit.txt"].action == Action.COPY_LR and cmp.by_path["Old/edit.txt"].evict_after
    _, res = p.sync()
    assert res.failed == 0
    assert tree(p.left) == {}
    assert (p.right / "Old/edit.txt").read_text() == "v2 edited"
    assert (p.right / "Old/new.txt").read_text() == "created locally"
    assert p.compare().pending() == []


def test_local_deletion_outside_selection_never_deletes_remote(pair, past):
    p = pair(selective="left")
    write(p.left / "Keep/a.txt", "a", past)
    write(p.left / "Keep/b.txt", "b", past)
    p.sync()
    _select(p, Keep=False)
    (p.left / "Keep/a.txt").unlink()  # freeing space by hand
    p.sync()
    assert tree(p.right) == {"Keep": "/", "Keep/a.txt": "a", "Keep/b.txt": "b"}
    assert tree(p.left) == {}


def test_conflict_outside_selection_is_kept(pair, past):
    p = pair(selective="left")
    write(p.left / "C/f.txt", "v1", past)
    p.sync()
    write(p.left / "C/f.txt", "left edit", past + 10)
    write(p.right / "C/f.txt", "right edit", past + 20)
    _select(p, C=False)
    cmp = p.compare()
    item = cmp.by_path["C/f.txt"]
    assert item.conflict and item.action == Action.SKIP
    assert Action.EVICT_L in item.allowed_actions()
    p.sync()
    assert (p.left / "C/f.txt").read_text() == "left edit"
    assert (p.right / "C/f.txt").read_text() == "right edit"


def test_stale_local_copy_goes_to_trash(pair, past):
    p = pair(selective="left")
    write(p.left / "S/f.txt", "old", past)
    p.sync()
    write(p.right / "S/f.txt", "newer on server", past + 50)
    _select(p, S=False)
    cmp = p.compare()
    assert cmp.by_path["S/f.txt"].action == Action.EVICT_L
    _, res = p.sync()
    assert res.failed == 0
    assert not (p.left / "S").exists()
    assert _trash_files(p.left)[-1].endswith("S/f.txt")
    assert (p.right / "S/f.txt").read_text() == "newer on server"


def test_folder_with_unsynchronized_content_is_kept(pair, past):
    p = pair(selective="left", exclude=["*.tmp"])
    write(p.left / "D/a.txt", "a", past)
    p.sync()
    write(p.left / "D/scratch.tmp", "not synchronized", past)
    _select(p, D=False)
    cmp, res = p.sync()
    assert res.failed == 0
    assert cmp.by_path["D"].status == "kept"
    assert tree(p.left) == {"D": "/", "D/scratch.tmp": "not synchronized"}
    assert (p.right / "D/a.txt").exists()


def test_checksum_mismatch_is_not_freed(pair, past):
    p = pair(selective="left")
    write(p.left / "X/f.bin", "AAAA", past)
    write(p.right / "X/f.bin", "BBBB", past)  # same size and time, different content
    _select(p, X=False)
    cmp, res = p.sync()
    item = cmp.by_path["X/f.bin"]
    assert item.status == "failed" and "differs" in item.error
    assert (p.left / "X/f.bin").read_text() == "AAAA"


def test_selected_folder_inside_unselected_one(pair, past):
    p = pair(selective="left")
    write(p.right / "P/old/x.txt", "x", past)
    write(p.right / "P/current/y.txt", "y", past)
    write(p.right / "P/readme.txt", "r", past)
    _select(p, P=False, P__current=True)
    p.sync()
    assert tree(p.left) == {"P": "/", "P/current": "/", "P/current/y.txt": "y"}
    assert p.compare().pending() == []


def test_single_file_can_be_deselected(pair, past):
    p = pair(selective="left")
    write(p.left / "movie.mkv", "m" * 100, past)
    write(p.left / "notes.txt", "n", past)
    p.sync()
    _select(p, **{"movie.mkv": False})
    p.sync()
    assert tree(p.left) == {"notes.txt": "n"}
    assert (p.right / "movie.mkv").exists()


def test_move_into_unselected_folder_is_renamed_then_freed(pair, past):
    p = pair(selective="left")
    write(p.left / "Active/doc.bin", "d" * 5000, past)
    os.makedirs(p.left / "Archive")
    write(p.left / "Archive/old.txt", "o", past)
    p.sync()
    _select(p, Archive=False)
    os.rename(p.left / "Active/doc.bin", p.left / "Archive/doc.bin")
    cmp = p.compare()
    item = cmp.by_path["Archive/doc.bin"]
    assert item.action == Action.MOVE_R and item.evict_after
    _, res = p.sync()
    assert res.bytes == 0 and res.failed == 0
    assert tree(p.left) == {"Active": "/"}
    assert (p.right / "Archive/doc.bin").exists() and not (p.right / "Active/doc.bin").exists()
    assert p.compare().pending() == []


# -- command line ------------------------------------------------------------

def test_cli_free_and_select(tmp_path, past, monkeypatch, capsys):
    from mysync import config as config_mod
    from mysync.cli import main

    left, right = tmp_path / "L", tmp_path / "R"
    write(left / "Projects/2019/a.bin", "a" * 100, past)
    write(left / "Projects/2026/b.txt", "b", past)
    write(left / "Other/c.txt", "c", past)
    path = config_mod.write_template("server", str(left), str(right))
    path.write_text(path.read_text().replace('# selective = "left"', 'selective = "left"'))
    job = config_mod.find_job("server")
    assert main(["sync", "-q", "server"]) == 0

    # dry run: nothing saved, nothing removed
    assert main(["free", "-n", "server", "Projects/2019"]) == 0
    assert "<fr" in capsys.readouterr().out
    assert selection.load(job).rules == {} and (left / "Projects/2019/a.bin").exists()

    # a local path, relative to the current directory, works too
    monkeypatch.chdir(left / "Projects")
    write(left / "Other/c.txt", "changed elsewhere", past + 100)  # not part of this run
    assert main(["free", "-q", "server", "2019"]) == 0
    assert not (left / "Projects/2019").exists()
    assert (right / "Projects/2019/a.bin").exists()
    assert (right / "Other/c.txt").read_text() == "c"  # only the freed path was synchronized
    assert selection.load(job).rules == {"Projects/2019": False}

    assert main(["select", "server", "--keep", "Projects/2019"]) == 0
    assert selection.load(job).rules == {}
    assert main(["sync", "-q", "server"]) == 0
    assert (left / "Projects/2019/a.bin").exists()


def test_cli_free_requires_selective_job(tmp_path, capsys):
    from mysync import config as config_mod
    from mysync.cli import main
    config_mod.write_template("plain", str(tmp_path / "a"), str(tmp_path / "b"))
    assert main(["free", "plain", "x"]) == 2
    assert "selective" in capsys.readouterr().err


def test_cli_select_applies_outer_paths_first(tmp_path):
    from mysync import config as config_mod
    from mysync.cli import main
    path = config_mod.write_template("server2", str(tmp_path / "L"), str(tmp_path / "R"))
    path.write_text(path.read_text().replace('# selective = "left"', 'selective = "left"'))
    assert main(["select", "server2", "--keep", "a/b", "c", "--drop", ""]) == 0
    assert selection.load(config_mod.find_job("server2")).rules == {"": False, "a/b": True, "c": True}
