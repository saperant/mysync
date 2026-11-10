from __future__ import annotations

import asyncio

import pytest

from mysync import config as config_mod
from mysync.model import Action
from mysync.tui.app import ConfirmScreen, JobScreen, JobsScreen, MysyncApp, PlanTable, SyncScreen

from conftest import tree, write

pytestmark = pytest.mark.asyncio


async def wait_for(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        await pilot.pause(0.05)
        if cond():
            return
    raise AssertionError("condition not reached")


async def test_jobs_compare_and_sync_with_mouse(tmp_path, past):
    left, right = tmp_path / "L", tmp_path / "R"
    write(left / "a.txt", "A", past)
    write(left / "skip-me.txt", "S", past)
    write(right / "b.txt", "B", past)
    config_mod.write_template("demo", str(left), str(right))

    app = MysyncApp()
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, JobsScreen)
        assert len(app.screen.jobs) == 1
        await pilot.click("#open")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, JobScreen)
        await wait_for(pilot, lambda: screen.cmp is not None)
        assert {i.path for i in screen.rows} == {"a.txt", "skip-me.txt", "b.txt"}

        # select "skip-me.txt" and press the Skip button
        row = [i.path for i in screen.rows].index("skip-me.txt")
        screen.query_one(PlanTable).move_cursor(row=row)
        await pilot.click("#act-SKIP")
        assert screen.cmp.by_path["skip-me.txt"].action == Action.SKIP

        await pilot.click("#sync")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#ok")
        await wait_for(pilot, lambda: isinstance(app.screen, SyncScreen)
                       and not app.screen.query_one("#close").disabled)
        await pilot.click("#close")
        await pilot.pause()
        assert app.screen is screen

    assert tree(right) == {"a.txt": "A", "b.txt": "B"}
    assert tree(left) == {"a.txt": "A", "b.txt": "B", "skip-me.txt": "S"}


async def test_action_cell_click_cycles(tmp_path, past):
    left, right = tmp_path / "L", tmp_path / "R"
    write(left / "a.txt", "A", past)
    right.mkdir()
    config_mod.write_template("demo", str(left), str(right))
    app = MysyncApp(initial_job="demo")
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, JobScreen)
        await wait_for(pilot, lambda: screen.cmp is not None)
        item = screen.cmp.by_path["a.txt"]
        assert item.action == Action.COPY_LR
        table = screen.query_one(PlanTable)
        # mark, left and path columns come first; click inside the action column of row 0
        x = sum(c.get_render_width(table) for c in table.ordered_columns[:3]) + 2
        await pilot.click(PlanTable, offset=(x, 1))
        await pilot.pause()
        assert item.action != Action.COPY_LR


async def test_selective_tree_with_mouse(tmp_path, past):
    from mysync import selection
    from mysync.tui.select import SelectScreen, SelectTree

    left, right = tmp_path / "L", tmp_path / "R"
    write(left / "Archive/old.bin", "o" * 2048, past)
    write(left / "Work/w.txt", "w", past)
    path = config_mod.write_template("demo", str(left), str(right))
    path.write_text(path.read_text().replace('# selective = "left"', 'selective = "left"'))
    job = config_mod.find_job("demo")
    from mysync.cli import main
    assert main(["sync", "-q", "demo"]) == 0

    app = MysyncApp(initial_job="demo", select=True)
    async with app.run_test(size=(120, 40)) as pilot:
        screen = app.screen
        assert isinstance(screen, SelectScreen)
        await wait_for(pilot, lambda: screen.nodes)
        tree_widget = screen.query_one(SelectTree)
        assert [n.data.path for n in tree_widget.root.children] == ["Archive", "Work"]
        # click the box in front of "Archive" (line 1: 3 guide cells + 2 arrow cells)
        await pilot.click(SelectTree, offset=(6, 1))
        await pilot.pause()
        assert not screen.sel.is_selected("Archive/old.bin") and screen.sel.is_selected("Work")
        assert "free 2.0 KiB" in str(screen.query_one("#summary").render())
        # clicking the name opens the folder instead
        await pilot.click(SelectTree, offset=(12, 2))
        await pilot.pause()
        assert tree_widget.root.children[1].is_expanded
        await pilot.click("#save")
        await pilot.pause()
        assert isinstance(app.screen, JobScreen)
        jobscreen = app.screen
        await wait_for(pilot, lambda: jobscreen.cmp is not None)
        assert jobscreen.cmp.by_path["Archive/old.bin"].action == Action.EVICT_L

    assert selection.load(job).rules == {"Archive": False}
    assert main(["sync", "-q", "demo"]) == 0
    assert tree(left) == {"Work": "/", "Work/w.txt": "w"}
    assert (right / "Archive/old.bin").exists()


async def test_mark_rows_and_choose_direction_with_keys(tmp_path, past):
    left, right = tmp_path / "L", tmp_path / "R"
    for name in ("a", "b", "c", "d"):
        write(left / f"{name}.txt", f"left {name}", past)
        write(right / f"{name}.txt", f"right {name}!", past + 100)  # conflicts without state
    write(right / "only-right.txt", "R", past)
    config_mod.write_template("demo", str(left), str(right))
    app = MysyncApp(initial_job="demo")
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        screen = app.screen
        await wait_for(pilot, lambda: screen.cmp is not None)
        paths = [i.path for i in screen.rows]
        assert paths == ["a.txt", "b.txt", "c.txt", "d.txt", "only-right.txt"]
        by = screen.cmp.by_path
        assert all(by[p].conflict and by[p].action == Action.SKIP for p in paths[:4])

        # no marks: the arrow key applies to the current row only
        await pilot.press("right")
        assert by["a.txt"].action == Action.COPY_LR and by["b.txt"].action == Action.SKIP

        # mark b and c with Shift+Down, then sync them to the left
        await pilot.press("down", "shift+down")
        assert screen.marked == {"b.txt", "c.txt"}
        await pilot.press("left")
        assert by["b.txt"].action == by["c.txt"].action == Action.COPY_RL
        assert by["a.txt"].action == Action.COPY_LR and by["d.txt"].action == Action.SKIP

        # Escape unmarks (and does not leave the screen); m marks and moves down
        await pilot.press("escape")
        assert not screen.marked and app.screen is screen
        await pilot.press("m", "m")  # c and d
        assert screen.marked == {"c.txt", "d.txt"}

        # Ctrl+click and Shift+click on rows mark ranges with the mouse
        await pilot.press("escape")
        await pilot.click(PlanTable, offset=(0, 1))  # mark column, row a
        await pilot.click(PlanTable, offset=(40, 5), shift=True)  # shift+click row only-right
        assert screen.marked == set(paths)
        # "to the right" for a right-only file means deleting it there
        await pilot.press("right")
        assert by["only-right.txt"].action == Action.DELETE_R
        assert all(by[p].action == Action.COPY_LR for p in paths[:4])
        assert app.screen is screen  # the clicks did not open the details dialog


async def test_retry_failed_button(tmp_path, past, monkeypatch):
    from mysync.backends.local import LocalBackend
    left, right = tmp_path / "L", tmp_path / "R"
    write(left / "a.txt", "A", past)
    write(left / "b.txt", "B", past)
    right.mkdir()
    path = config_mod.write_template("demo", str(left), str(right))
    path.write_text(path.read_text().replace("retries = 2", "retries = 0"))
    real, calls = LocalBackend.write_file, []

    def flaky(self, rel, *args, **kwargs):
        calls.append(rel)
        if len(calls) == 1:
            raise OSError("network hiccup")
        return real(self, rel, *args, **kwargs)
    monkeypatch.setattr(LocalBackend, "write_file", flaky)

    app = MysyncApp("demo")
    async with app.run_test(size=(160, 45)) as pilot:
        await wait_for(pilot, lambda: isinstance(app.screen, JobScreen) and app.screen.cmp is not None)
        await pilot.click("#sync")
        await pilot.pause()
        await pilot.click("#ok")
        await wait_for(pilot, lambda: isinstance(app.screen, SyncScreen)
                       and not app.screen.query_one("#close").disabled)
        dialog = app.screen
        assert dialog.result.failed == 1
        assert not dialog.query_one("#retry").disabled
        await pilot.click("#retry")
        await wait_for(pilot, lambda: dialog.result is not None and not dialog.query_one("#close").disabled)
        assert dialog.result.failed == 0 and dialog.result.done == 2
        assert dialog.query_one("#retry").disabled
        await pilot.click("#close")
    assert tree(right) == {"a.txt": "A", "b.txt": "B"}


async def test_space_ignores_a_folder_for_this_round(tmp_path, past):
    left, right = tmp_path / "L", tmp_path / "R"
    write(left / "a.txt", "A", past)
    write(left / "big/x.bin", "X", past)
    write(left / "big/y.bin", "Y", past)
    right.mkdir()
    config_mod.write_template("demo", str(left), str(right))
    app = MysyncApp(initial_job="demo")
    async with app.run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        screen = app.screen
        await wait_for(pilot, lambda: screen.cmp is not None)
        table = screen.query_one(PlanTable)
        table.move_cursor(row=[i.path for i in screen.rows].index("big"))
        await pilot.press("space")
        await pilot.pause()
        assert [i.path for i in screen.rows] == ["a.txt"]
        assert screen.cmp.by_path["big/x.bin"].ignored
        # shown again with the filter, and Space takes it back
        screen.query_one("#f-ignored").value = True
        await pilot.pause()
        table.move_cursor(row=[i.path for i in screen.rows].index("big"))
        await pilot.press("space")
        await pilot.pause()
        assert not screen.cmp.by_path["big/x.bin"].ignored
        assert screen.cmp.by_path["big/x.bin"].action == Action.COPY_LR
        await pilot.press("space")  # cursor moved down to big/x.bin: ignore just that file
        await pilot.pause()
        assert screen.cmp.by_path["big/x.bin"].ignored and not screen.cmp.by_path["big"].ignored
        # "a" cycles the action now
        table.move_cursor(row=[i.path for i in screen.rows].index("a.txt"))
        await pilot.press("a")
        await pilot.pause()
        assert screen.cmp.by_path["a.txt"].action != Action.COPY_LR
