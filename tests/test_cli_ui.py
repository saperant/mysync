"""`mysync` and `mysync ui`: which workbench, which job, and the workbench's own options."""

import os

import pytest

from mysync import cli
from mysync.config import write_template


@pytest.fixture
def started(monkeypatch):
    """What mysync.ui.run was called with (instead of replacing the test process)."""
    calls = []
    monkeypatch.setattr("mysync.ui.run", lambda args=(): calls.append(list(args)))
    for name in ("MYSYNC_OPEN", "MYRCP_GTK"):  # set by the commands: restored afterwards
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    return calls


def test_no_command_starts_the_workbench(started):
    assert cli.main([]) == 0
    assert started == [[]]


def test_ui_opens_a_job_and_passes_options_on(started, tmp_path, monkeypatch):
    write_template("photos", str(tmp_path / "a"), str(tmp_path / "b"))
    assert cli.main(["ui", "photos", "--gtk", "--", "--list"]) == 0
    assert started == [["--list"]]
    assert os.environ["MYSYNC_OPEN"] == "job:photos" and os.environ["MYRCP_GTK"] == "1"


def test_select_without_options_opens_the_selection(started, tmp_path, monkeypatch):
    path = write_template("photos", str(tmp_path / "a"), str(tmp_path / "b"))
    path.write_text('selective = "left"\n' + path.read_text())
    assert cli.main(["select", "photos"]) == 0
    assert os.environ["MYSYNC_OPEN"] == "select:photos" and started == [[]]


def test_unknown_job_is_an_error(started, capsys):
    assert cli.main(["ui", "nope"]) == cli.EXIT_FATAL
    assert started == [] and "nope" in capsys.readouterr().err
