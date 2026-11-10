"""mysync in the myrcp workbench: the job list, and a tab per job with its plan.

mysync's user interface: the engine (compare, sync, selection) presented through the
myrcp UI IR, drawn by the terminal or the GTK workbench.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from myrcp import Item as SelItem
from myrcp import Job, Marker, Plugin, View, ui

try:
    from mysync import config as config_mod
    from mysync import fmt, hooks, paths
    from mysync import state as state_mod
    from mysync.backends import open_backend
    from mysync.compare import Comparison, Item, compare, toward
    from mysync.model import Action, Category, SyncError
    from mysync.runlog import RunLog
    from mysync.sync import Reporter, Synchronizer, SyncResult, describe
except ImportError as e:
    raise ImportError(
        f"mysync is not importable from {sys.executable} ({e}). Start the workbench with "
        "`mysync`, or set MYSYNC_PYTHON to a Python that has mysync."
    ) from e

plugin = Plugin()

# mysync's colours as the workbench's semantic styles.
ACTION_STYLE = {
    Action.NONE: "dim",
    Action.COPY_LR: "ok",
    Action.COPY_RL: "accent",
    Action.MOVE_R: "special",
    Action.MOVE_L: "special",
    Action.ATTR_LR: "info",
    Action.ATTR_RL: "info",
    Action.DELETE_L: "error",
    Action.DELETE_R: "error",
    Action.EVICT_L: "warning",
    Action.EVICT_R: "warning",
    Action.SKIP: "warning",
}
ACTION_TEXT = {
    Action.NONE: "  =  ",
    Action.COPY_LR: " ──▶ ",
    Action.COPY_RL: " ◀── ",
    Action.MOVE_R: " mv▶ ",
    Action.MOVE_L: " ◀mv ",
    Action.ATTR_LR: " at▶ ",
    Action.ATTR_RL: " ◀at ",
    Action.DELETE_L: " ✖◀  ",
    Action.DELETE_R: "  ▶✖ ",
    Action.EVICT_L: " ⏏◀  ",
    Action.EVICT_R: "  ▶⏏ ",
    Action.SKIP: "  ?  ",
}
CATEGORY_STYLE = {
    Category.EQUAL: "dim",
    Category.LEFT_ONLY: "ok",
    Category.RIGHT_ONLY: "accent",
    Category.LEFT_NEWER: "ok",
    Category.RIGHT_NEWER: "accent",
    Category.DIFFERENT: "special",
    Category.ATTRIBUTES: "info",
    Category.CONFLICT: "warning",
}


def cell(text: str, style: str = "normal") -> dict[str, str]:
    return {"text": text, "style": style}


def action_cell(item: Item) -> dict[str, str]:
    if item.ignored:
        return cell("  –  ", "dim")
    if item.status == "done":
        return cell("  ✔  ", "ok")
    if item.status == "failed":
        return cell("  ✘  ", "error")
    if item.status == "changed":
        return cell("  ≠  ", "warning")
    if item.status == "kept":
        return cell("  ⏏  ", "dim")
    if item.moved_away:
        return cell("  ↳  ", "dim")
    if item.via_move:
        return cell("  ⋯  ", "dim")
    if (
        item.action == Action.NONE and item.name_fix
    ):  # only the spelling of the name changes
        return cell(" Aa▶ " if item.name_fix == "right" else " ◀Aa ", "special")
    return cell(ACTION_TEXT[item.action], ACTION_STYLE[item.action])


def entry_cell(item: Item, side: str) -> dict[str, str]:
    entry = item.left if side == "left" else item.right
    covered = item.covered and item.action == (
        Action.DELETE_L if side == "left" else Action.DELETE_R
    )
    text = fmt.entry(entry)
    if covered and text:  # deleted along with its folder
        text = "(" + text + ")"
    return cell(text, "dim" if item.ignored else CATEGORY_STYLE[item.category])


def path_cell(item: Item, tree: bool = False) -> dict[str, str]:
    """The path; in the tree its last part, the folders above being the rows above."""
    name = (item.path.rsplit("/", 1)[-1] if tree else item.path) + (
        "/" if item.is_dir else ""
    )
    if item.ignored or item.moved_away:
        return cell(name, "dim")
    if item.conflict:
        return cell(name, "warning")
    return cell(name, "bold" if item.is_dir else "normal")


def reason_cell(item: Item) -> dict[str, str]:
    if item.error:
        return cell(item.error, "error")
    if item.held:
        return cell("kept: holds ignored items", "dim")
    return cell(item.reason, "dim")


def _in_move(i: Item) -> bool:
    return i.via_move or i.moved_away


FILTERS: list[tuple[str, str, Callable[[Item], bool]]] = [
    (
        "f-copy-r",
        "──▶ to right",
        lambda i: not _in_move(i) and i.action in (Action.COPY_LR, Action.ATTR_LR),
    ),
    (
        "f-copy-l",
        "◀── to left",
        lambda i: not _in_move(i) and i.action in (Action.COPY_RL, Action.ATTR_RL),
    ),
    (
        "f-move",
        "mv renames",
        lambda i: (i.action.is_move and not i.via_move) or bool(i.name_fix),
    ),
    (
        "f-delete",
        "✖ deletes/⏏ frees",
        lambda i: (
            (i.action.is_delete and not i.moved_away)
            or i.action.is_evict
            or i.evict_after
        ),
    ),
    ("f-conflict", "? conflicts", lambda i: i.action == Action.SKIP or i.conflict),
    ("f-equal", "= equal/details", lambda i: i.action == Action.NONE or _in_move(i)),
    ("f-ignored", "– ignored", lambda i: i.ignored),
]

PLAN_COLUMNS = [
    ui.column("", width=1, clickable=True),  # a click marks the row
    ui.column("Left", width=31),
    ui.column("Path", grow=2),
    ui.column("", width=5, clickable=True),  # a click cycles the action
    ui.column("Right", width=31),
    ui.column("Reason", grow=1),
]

Spans = list[tuple[str, str] | str]

COMPARE_LIMIT = 64 * 1024 * 1024  # bigger files are not fetched for a compare


def hints(*pairs: str) -> ui.Node:
    """Key hints: hints("c", "compare", "s", "sync")."""
    spans: Spans = []
    for k, what in zip(pairs[::2], pairs[1::2], strict=True):
        spans += [(k, "accent"), f" {what}   "]
    return ui.text(spans, "dim")


# ---------------------------------------------------------------------------------
# the job list


@plugin.view("mysync.jobs")
class Jobs(View):
    def setup(self) -> None:
        self.jobs: list[config_mod.JobConfig] = []
        self.errors: list[str] = []
        self.cursor_key: str | None = None
        self.new_job: dict[str, Any] | None = None  # the "new job" dialog, while open
        self.reload()
        # `mysync ui JOB [--select]`: that job's tab (or its selection), once.
        kind, _, name = os.environ.pop("MYSYNC_OPEN", "").partition(":")
        if name and kind in ("job", "select"):
            self.rcp.open_view("mysync.job" if kind == "job" else "mysync.select", name)

    @plugin.command("mysync.jobs.reload")
    def reload(self) -> None:
        result = config_mod.load_all()
        self.jobs = result.jobs
        self.errors = result.errors

    def _current(self) -> config_mod.JobConfig | None:
        key = (
            self.focus_key
            if self.focus_widget == "jobs" and self.focus_key
            else self.cursor_key
        )
        for job in self.jobs:
            if job.name == key:
                return job
        return self.jobs[0] if self.jobs else None

    def render(self) -> ui.Node:
        rows = []
        for job in self.jobs:
            last = state_mod.last_sync_time(job)
            rows.append(
                ui.row(
                    job.name,
                    [
                        cell(job.name, "bold"),
                        job.mode,
                        job.left.url,
                        job.right.url,
                        fmt.mtime(last) if last else cell("never", "dim"),
                    ],
                    selection=SelItem(
                        "mysync.job",
                        job.name,
                        job.file,
                        1 if job.file else 0,
                        {
                            "Mode": job.mode,
                            "Left": job.left.url,
                            "Right": job.right.url,
                            "Compare": job.compare,
                            "Conflicts": job.conflict,
                            "Deletion": job.deletion,
                            "Last sync": fmt.mtime(last) if last else "never",
                            "Description": job.description or "—",
                        },
                    ),
                )
            )
        base = ui.vbox(
            ui.text([("Jobs in ", "dim"), (str(paths.conf_dir()), "bold")]),
            ui.text(""),
            ui.table(
                "jobs",
                [
                    ui.column("Job", width=16),
                    ui.column("Mode", width=8),
                    ui.column("Left"),
                    ui.column("Right"),
                    ui.column("Last sync", width=17),
                ],
                rows,
                empty="No jobs yet. Press n to create one.",
                sortable=True,
                on_select=self._select,
                on_activate=self.open,
            ),
            *[ui.text(e, "error", wrap=True) for e in self.errors],
            ui.hbox(
                ui.button("open", "Open", on_press=lambda: self.open(None)),
                ui.button("new", "New job", on_press=self.new),
                ui.button("edit", "Edit", on_press=self.edit),
                ui.button("reload", "Reload", on_press=self.reload),
            ),
            hints("Enter", "open", "n", "new job", "e", "edit", "r", "reload"),
        )
        if self.new_job is None:
            return base
        return ui.modal(
            "new-job",
            base,
            self._new_dialog(),
            title="New synchronization job",
            on_dismiss=self._close_new,
        )

    def _select(self, key: str) -> None:
        self.cursor_key = key

    def open(self, key: str | None) -> None:
        job = (
            self._current()
            if key is None
            else next((j for j in self.jobs if j.name == key), None)
        )
        if job:
            self.rcp.open_view("mysync.job", job.name)

    @plugin.command("mysync.jobs.edit")
    def edit(self) -> None:
        job = self._current()
        if job and job.file:
            self.rcp.open_in_editor(job.file, on_close=lambda waited: self.reload())

    # -- new job -------------------------------------------------------------------
    @plugin.command("mysync.jobs.new")
    def new(self) -> None:
        self.new_job = {"mode": "two-way", "error": ""}

    def _close_new(self) -> None:
        self.new_job = None

    def _new_dialog(self) -> ui.Node:
        d = self.new_job or {}

        def mode_box(mode: str) -> ui.Node:
            return ui.checkbox(
                f"mode-{mode}",
                mode,
                d["mode"] == mode,
                on_toggle=lambda _on: d.update(mode=mode),
            )

        return ui.vbox(
            ui.text("Name"),
            ui.field("name", placeholder="photos"),
            ui.text("Left location"),
            ui.field("left", placeholder="~/Pictures"),
            ui.text(
                "Right location (path, sftp://host/path, ssh://host/path or rclone:remote:path)",
                wrap=True,
            ),
            ui.field("right", placeholder="sftp://server/srv/photos"),
            ui.text("Mode"),
            ui.hbox(*[mode_box(m) for m in config_mod.MODES], gap=3),
            ui.text(d["error"], "error", wrap=True) if d["error"] else ui.text(""),
            ui.hbox(
                ui.button(
                    "create", "Create", on_press=lambda: self._create(edit=False)
                ),
                ui.button(
                    "create-edit",
                    "Create & edit",
                    on_press=lambda: self._create(edit=True),
                ),
                ui.button("cancel", "Cancel", on_press=self._close_new),
            ),
        )

    def _create(self, edit: bool) -> None:
        d = self.new_job
        if d is None:
            return
        name, left, right = (
            self.form.get(k, "").strip() for k in ("name", "left", "right")
        )
        mode = d["mode"]
        try:
            if not (name and left and right):
                raise config_mod.ConfigError("name, left and right are required")
            config_mod.job_from_dict({"left": left, "right": right, "mode": mode}, name)
            path = config_mod.write_template(name, left, right)
            if mode != "two-way":
                path.write_text(
                    path.read_text().replace('mode = "two-way"', f'mode = "{mode}"', 1)
                )
        except config_mod.ConfigError as e:
            d["error"] = str(e)
            return
        self.new_job = None
        self.reload()
        self.rcp.status(f"Created {path}", "ok")
        if edit:
            self.rcp.open_in_editor(path, on_close=lambda waited: self.reload())


# ---------------------------------------------------------------------------------
# a job


class _SyncRun:
    """One synchronization (and its retries), as the progress dialog shows it."""

    def __init__(self, cmp: Comparison, force: bool) -> None:
        self.cmp = cmp
        self.force = force
        self.lock = threading.Lock()
        self.syncer: Synchronizer | None = None
        self.reporter: _Reporter | None = None
        self.running = False
        self.result: SyncResult | None = None
        self.error = ""
        self.log_path = ""
        self.lines: list[str] = []
        self.current = ""
        self.reset(len(cmp.pending()), cmp.transfer_bytes())

    def reset(self, items: int, nbytes: int) -> None:
        self.total_items, self.total_bytes = items, nbytes
        self.items_done = self.bytes_done = 0
        self.started = time.monotonic()


class _Reporter(Reporter):
    def __init__(self, view: JobView, run: _SyncRun) -> None:
        super().__init__()
        self.view = view
        self.run = run
        self.runlog: RunLog | None = None
        self.job: Job | None = None
        self._last = 0.0

    def log(self, message: str) -> None:
        if self.runlog:
            self.runlog.write(message)
        with self.run.lock:
            self.run.lines.append(message)
            del self.run.lines[:-2000]
        self._poke()

    def item_start(self, item: Item, description: str) -> None:
        self.run.current = f"{description}: {item.path}"
        self._poke()

    def item_done(self, item: Item) -> None:
        with self.run.lock:
            self.run.items_done += 1
        self._poke()

    def transferred(self, nbytes: int) -> None:
        with self.run.lock:
            self.run.bytes_done += nbytes
        self._poke()

    def _poke(self) -> None:
        now = time.monotonic()
        if now - self._last < 0.1:
            return
        self._last = now
        r = self.run
        if self.job:
            done = (
                r.bytes_done / r.total_bytes
                if r.total_bytes
                else r.items_done / max(r.total_items, 1)
            )
            self.job.progress(done, f"{r.items_done}/{r.total_items} items")
        self.view.invalidate()


_JOB_VIEWS: dict[str, JobView] = {}  # open job tabs by job name


@plugin.view("mysync.job")
class JobView(View):
    def setup(self) -> None:
        if not self.arg:
            raise ValueError("open a job from the Sync jobs view")
        self.job = config_mod.find_job(self.arg)
        _JOB_VIEWS[self.job.name] = self
        self.job_mtime = self._mtime()
        self.cmp: Comparison | None = None
        self.rows: list[Item] = []
        self.index: dict[str, int] = {}
        # The plan as a tree of folders (or the flat list of paths): which folders are
        # closed, and each row's depth. kids: a folder's items, "" the top.
        self.tree = True
        self.kids: dict[str, list[Item]] = {}
        self.closed: set[str] = set()
        self.depth: list[int] = []
        self.has_kids: set[str] = set()  # folders with shown rows below them
        self.shown: set[str] = set()  # the rows the filters let through
        self.sort: tuple[int, bool] | None = None
        self.marked: set[str] = set()
        self.anchor: int | None = None
        self.cursor = ui.CursorRequest()
        self.cursor_key: str | None = None
        self.cursor_index = 0
        self.filters = {
            fid: fid not in ("f-equal", "f-ignored") for fid, _, _ in FILTERS
        }
        self.summary: list[Spans] = [
            [("Press c (or F5) to compare both locations.", "dim")]
        ]
        self.comparing: Job | None = None
        self.dialog: str | None = None  # "details" | "confirm" | "sync"
        self.detail: Item | None = None
        self.run: _SyncRun | None = None
        self.tmp: Path | None = None  # copies fetched for comparing, gone with the view
        self.fetches = 0
        self.compare()

    def title(self) -> str:
        return f"⇄ {self.job.name}"

    def dispose(self) -> None:
        if _JOB_VIEWS.get(self.job.name) is self:
            del _JOB_VIEWS[self.job.name]
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.rcp.set_markers(f"conflicts:{self.job.name}", [])

    def _report_conflicts(self) -> None:
        """Unresolved conflicts as problem markers: the view shows them, a local file too."""
        if self.cmp is None:
            return
        markers = []
        for item in self.cmp.items:
            if not (item.conflict and item.action == Action.SKIP and not item.ignored):
                continue
            path = None
            if self.job.left.scheme == "local" and not item.is_dir:
                path = Path(self.job.left.path) / item.path
            message = f"{self.job.name}: {item.path}: {item.reason or 'conflict'}"
            markers.append(
                Marker(
                    message,
                    path,
                    0,
                    0,
                    "warning",
                    "mysync",
                    f"mysync.job#{self.job.name}",
                )
            )
        self.rcp.set_markers(f"conflicts:{self.job.name}", markers)

    def _mtime(self) -> float:
        try:
            return self.job.file.stat().st_mtime if self.job.file else 0.0
        except OSError:
            return 0.0

    # -- rendering ---------------------------------------------------------------
    def render(self) -> ui.Node:
        j = self.job
        info = (
            f"  {j.mode}, compare by {j.compare}, conflicts: {j.conflict}, deletion: {j.deletion}"
            + (
                f", selective sync: the {j.selective} keeps a selection"
                if j.selective
                else ""
            )
        )
        base = ui.vbox(
            ui.text([(j.name, "bold"), (info, "dim")]),
            ui.text([("L ", "ok"), j.left.url, "\n", ("R ", "accent"), j.right.url]),
            ui.hbox(
                *[
                    ui.checkbox(
                        fid, label, self.filters[fid], on_toggle=self._filter(fid)
                    )
                    for fid, label, _ in FILTERS
                ],
                gap=2,
            ),
            ui.table(
                "plan",
                PLAN_COLUMNS,
                ui.lazy_rows(len(self.rows), self._row),
                cursor=self.cursor,
                autofocus=True,
                empty="Comparing…"
                if self.comparing
                else "Nothing to show with these filters.",
                on_select=self._select,
                on_activate=self._activate,
                on_cell=self._cell,
                on_sort=self._sort,
                tree_column=2 if self.tree else None,
                on_expand=self._expand,
                on_collapse=self._collapse,
            ),
            ui.text(self._summary_spans()),
            hints(
                "c",
                "compare",
                "s",
                "sync",
                "</>",
                "direction",
                "-",
                "skip",
                "←/→",
                "fold",
                "f",
                "flat" if self.tree else "tree",
                "m",
                "mark",
                "space",
                "ignore",
                "a",
                "cycle",
                "Enter",
                "details",
                "=",
                "compare",
                "n",
                "newer wins",
                "e",
                "edit job",
            ),
        )
        if self.dialog == "details" and self.detail is not None:
            return ui.modal(
                "details",
                base,
                self._details_dialog(self.detail),
                title="Item",
                on_dismiss=self._close_dialog,
            )
        if self.dialog == "confirm":
            return ui.modal(
                "confirm",
                base,
                self._confirm_dialog(),
                title="Start synchronization?",
                on_dismiss=self._close_dialog,
            )
        if self.dialog == "sync" and self.run is not None:
            return ui.modal(
                "sync",
                base,
                self._sync_dialog(self.run),
                title=f"Synchronizing {j.name}",
                on_dismiss=self._sync_escape,
                width=100,
            )
        return base

    def _row(self, i: int) -> ui.Node:
        item = self.rows[i]
        if not self.tree:
            return ui.row(item.path, self._cells(item), selection=self._sel_item(item))
        folder = item.path in self.has_kids
        return ui.row(
            item.path,
            self._cells(item),
            selection=self._sel_item(item),
            depth=self.depth[i],
            expandable=folder,
            expanded=folder and item.path not in self.closed,
        )

    def _cells(self, item: Item) -> list[dict[str, str]]:
        marked = item.path in self.marked
        return [
            cell("■", "warning") if marked else cell("·", "dim"),
            entry_cell(item, "left"),
            path_cell(item, self.tree),
            action_cell(item),
            entry_cell(item, "right"),
            reason_cell(item),
        ]

    def _sel_item(self, item: Item) -> SelItem:
        """A compared path; placed at its local copy when the left side is a local folder."""
        j = self.job
        local = None
        if j.left.scheme == "local" and not item.is_dir:
            p = Path(j.left.path) / item.path
            local = p if p.exists() else None
        return SelItem(
            "mysync.path",
            item.path + ("/" if item.is_dir else ""),
            local,
            props={
                "Left": fmt.entry(item.left) or "—",
                "Right": fmt.entry(item.right) or "—",
                "Action": item.action.value,
                "Category": item.category.value,
                "Reason": item.error or item.reason or "—",
                "Conflict": "yes" if item.conflict else "no",
                "Marked": "yes" if item.path in self.marked else "no",
                "Job": j.name,
            },
        )

    def _summary_spans(self) -> Spans:
        out: Spans = []
        for n, line in enumerate(self.summary):
            if n:
                out.append("\n")
            out.extend(line)
        return out

    def _update_summary(self) -> None:
        cmp = self.cmp
        if cmp is None:
            return
        c = cmp.counts()
        line: Spans = [
            (f"──▶ {c[Action.COPY_LR.value]}", "ok"),
            "   ",
            (f"◀── {c[Action.COPY_RL.value]}", "accent"),
            "   ",
            (f"mv {c[Action.MOVE_R.value] + c[Action.MOVE_L.value]}", "special"),
            "   ",
            (f"attr {c[Action.ATTR_LR.value] + c[Action.ATTR_RL.value]}", "info"),
            "   ",
        ]
        if c["names"]:
            line += [(f"Aa {c['names']}", "special"), "   "]
        line += [
            (f"✖◀ {c[Action.DELETE_L.value]}   ▶✖ {c[Action.DELETE_R.value]}", "error"),
            "   ",
            (f"conflicts {c['conflict']}", "warning"),
            f"   skip {c[Action.SKIP.value]}   ",
            f"transfer {fmt.size(cmp.transfer_bytes())}   ",
        ]
        if self.job.selective:
            line += [(f"⏏ free {fmt.size(cmp.freed_bytes())}", "warning"), "   "]
        if c["ignored"]:
            line += [(f"– {c['ignored']} ignored", "dim"), "   "]
        if self.marked:
            line += [(f"■ {len(self._marked_rows())} marked", "warning"), "   "]
        # On a line of its own: the counts above already fill a wide terminal.
        lines = [
            line,
            [
                (
                    f"{len(self.rows)} shown of {len(cmp.items)}; {cmp.state_message}",
                    "dim",
                )
            ],
        ]
        problem = cmp.safety_problem()
        if problem:
            lines.append([(f"⚠ {problem}", "error")])
        if cmp.warnings:
            more = (
                f" (and {len(cmp.warnings) - 1} more)" if len(cmp.warnings) > 1 else ""
            )
            lines.append([(f"⚠ {cmp.warnings[0]}{more}", "warning")])
        self.summary = lines

    # -- comparing ---------------------------------------------------------------
    def _reload_if_edited(self) -> None:
        if self.job.file is None or self._mtime() == self.job_mtime:
            return
        try:
            self.job = config_mod.load_job_file(
                self.job.file, config_mod.load_global_defaults()
            )
            self.job_mtime = self._mtime()
        except config_mod.ConfigError as e:
            self.rcp.status(str(e), "error")

    @plugin.command("mysync.job.compare")
    def compare(self) -> None:
        if self.comparing or (self.run and self.run.running):
            return
        self._reload_if_edited()
        job = self.job
        self.summary = [[("Connecting…", "dim")]]

        def work(j: Job) -> Comparison:
            def stage(msg: str) -> None:
                self.summary = [[("Comparing: ", "bold"), msg]]
                j.progress(0, msg)
                self.invalidate()

            with open_backend(job.left) as lb, open_backend(job.right) as rb:
                return compare(job, lb, rb, stage)

        self.comparing = self.rcp.run_job(
            f"Compare {job.name}", work, on_done=self._compared
        )

    def _compared(self, j: Job) -> None:
        self.comparing = None
        if j.ok:
            self.cmp = j.result
            self.marked.clear()
            self._index_tree()
            self._refresh(keep_cursor=False)
            self._report_conflicts()
        else:
            self.summary = [
                [("Comparison failed: ", "error"), str(j.error or "cancelled")]
            ]

    # -- the plan table --------------------------------------------------------------
    def _index_tree(self) -> None:
        """Each folder's items; a folder whose content goes with it (new on one side,
        gone, renamed) starts closed, the rest open."""
        assert self.cmp is not None
        by_path = self.cmp.by_path
        self.kids = {}
        self.closed = set()
        for item in self.cmp.items:
            parent = item.path
            while True:  # the nearest folder there is an item for
                parent = parent.rsplit("/", 1)[0] if "/" in parent else ""
                if not parent or parent in by_path:
                    break
            self.kids.setdefault(parent, []).append(item)
            if item.is_dir and (
                item.category in (Category.LEFT_ONLY, Category.RIGHT_ONLY)
                or item.action.is_move
                or item.moved_away
            ):
                self.closed.add(item.path)

    def _sort_text(self, item: Item, column: int) -> str:
        c = self._cells(item)[column]
        return str(c["text"]).strip("()/ ")

    def _ordered(self, items: list[Item]) -> list[Item]:
        """By the column sorted by (empty cells last either way), else as mysync lists
        them; in the tree, folders before files."""
        if self.sort is not None:
            col, desc = self.sort
            keys = {id(i): ui.sort_key(self._sort_text(i, col)) for i in items}
            items = sorted(items, key=lambda i: keys[id(i)], reverse=desc)
            if desc:
                items.sort(key=lambda i: keys[id(i)][0])
        if self.tree and self.sort is None:
            items = sorted(items, key=lambda i: (not i.is_dir, i.path.casefold()))
        return items

    def _refresh(self, keep_cursor: bool = True) -> None:
        self.rows = []
        self.depth = []
        shown: list[Item] = []
        if self.cmp is not None:
            preds = [pred for fid, _, pred in FILTERS if self.filters[fid]]
            show_ignored = self.filters["f-ignored"]
            for item in self.cmp.items:
                if item.ignored and not show_ignored:
                    continue
                if not item.ignored and not any(p(item) for p in preds):
                    continue
                shown.append(item)
        self.shown = {i.path for i in shown}
        if not self.tree:
            self.rows = self._ordered(shown)
        else:
            # The rows shown and the folders above them, depth first.
            need = set(self.shown)
            self.has_kids = set()
            by_path = self.cmp.by_path if self.cmp else {}
            for item in shown:
                p = item.path
                while "/" in p:
                    p = p.rsplit("/", 1)[0]
                    if p not in by_path:
                        continue
                    self.has_kids.add(p)
                    if p in need:
                        break
                    need.add(p)
            stack = [
                (i, 0)
                for i in reversed(
                    self._ordered([i for i in self.kids.get("", []) if i.path in need])
                )
            ]
            while stack:
                item, depth = stack.pop()
                self.rows.append(item)
                self.depth.append(depth)
                if item.path in self.has_kids and item.path not in self.closed:
                    below = [i for i in self.kids.get(item.path, []) if i.path in need]
                    stack.extend((i, depth + 1) for i in reversed(self._ordered(below)))
        self.index = {item.path: n for n, item in enumerate(self.rows)}
        if not keep_cursor:
            self._move(0)
        elif (
            self.cursor_key in self.index
            and self.index[self.cursor_key] != self.cursor_index
        ):
            self._move(self.index[self.cursor_key])  # rows came or went above it
        self._update_summary()

    # -- the tree ------------------------------------------------------------------------
    def _expand(self, key: str) -> None:
        self._select(key)
        if key not in self.has_kids:
            return
        if key in self.closed:
            self.closed.discard(key)
            self._refresh()
        elif (n := self.index.get(key)) is not None and n + 1 < len(self.rows):
            self._move(n + 1)  # open already: to its first row

    def _collapse(self, key: str) -> None:
        self._select(key)
        if key in self.has_kids and key not in self.closed:
            self.closed.add(key)
            self._refresh()
            return
        p = key  # closed already, or a file: to the folder above
        while "/" in p:
            p = p.rsplit("/", 1)[0]
            if p in self.index:
                self._move(self.index[p])
                return

    @plugin.command("mysync.job.expandAll")
    def expand_all(self) -> None:
        self.closed.clear()
        self._refresh()

    @plugin.command("mysync.job.collapseAll")
    def collapse_all(self) -> None:
        self.closed = set(self.has_kids)
        top = self._current()
        while top is not None and "/" in top.path:  # the cursor goes to its top folder
            p = top.path.rsplit("/", 1)[0]
            top = self.cmp.by_path.get(p) if self.cmp else None
        if top is not None:
            self.cursor_key = top.path
        self._refresh()

    @plugin.command("mysync.job.tree")
    def toggle_tree(self) -> None:
        """The plan as a tree of folders, or as a flat list of paths."""
        self.tree = not self.tree
        self._refresh()

    def _sort(self, column: int, descending: bool) -> None:
        self.sort = (column, descending) if column >= 0 else None
        self._refresh()

    def _filter(self, fid: str) -> Callable[[bool], None]:
        def toggle(on: bool) -> None:
            self.filters[fid] = on
            self._refresh(keep_cursor=False)

        return toggle

    def _move(self, index: int) -> None:
        self.cursor_index = max(0, min(index, len(self.rows) - 1))
        self.cursor_key = self.rows[self.cursor_index].path if self.rows else None
        self.cursor.move(self.cursor_index)

    def _select(self, key: str) -> None:
        self.cursor_key = key
        self.cursor_index = self.index.get(key, self.cursor_index)

    def _activate(self, key: str) -> None:
        self._select(key)
        self.details()

    def _cell(self, click: dict[str, Any]) -> None:
        """Mouse: the mark column (or Ctrl+click) marks, Shift+click marks a range from
        the last marked row, the action column cycles the action."""
        key = str(click.get("key"))
        n = self.index.get(key)
        if n is None:
            return
        self._select(key)
        self.focus_widget, self.focus_key = "plan", key
        if click.get("shift") and self.anchor is not None:
            lo, hi = sorted((self.anchor, n))
            self._set_marks(list(range(lo, hi + 1)), True)
        elif click.get("column") == 0 or click.get("ctrl") or click.get("shift"):
            self._set_marks([n])
        else:
            self.cycle()
            return
        self.anchor = n

    def _current_index(self) -> int | None:
        if not self.rows:
            return None
        if self.focus_widget == "plan" and self.focus_key in self.index:
            return self.index[self.focus_key]
        if self.cursor_key in self.index:
            return self.index[self.cursor_key]
        return min(self.cursor_index, len(self.rows) - 1)

    def _current(self) -> Item | None:
        n = self._current_index()
        return None if n is None else self.rows[n]

    # -- marking -------------------------------------------------------------------
    def _marked_rows(self) -> list[Item]:
        return [i for i in self.rows if i.path in self.marked]

    def _targets(self) -> list[Item]:
        """Marked rows that are shown, or else the current row. A closed folder in the
        tree stands for what the filters show of its content as well."""
        marked = self._marked_rows()
        if not marked:
            item = self._current()
            marked = [item] if item else []
        if not self.tree or self.cmp is None:
            return marked
        out: dict[str, Item] = {}
        for item in marked:
            out[item.path] = item
            if item.path in self.has_kids and item.path in self.closed:
                for d in self.cmp.descendants(item):
                    if d.path in self.shown:
                        out.setdefault(d.path, d)
        return list(out.values())

    def _set_marks(self, rows: list[int], value: bool | None = None) -> None:
        for n in rows:
            if 0 <= n < len(self.rows):
                path = self.rows[n].path
                on = (path not in self.marked) if value is None else value
                (self.marked.add if on else self.marked.discard)(path)
        self._update_summary()

    @plugin.command("mysync.job.mark")
    def mark(self) -> None:
        n = self._current_index()
        if n is None:
            return
        self._set_marks([n])
        self.anchor = n
        self._move(n + 1)

    def _extend(self, step: int) -> None:
        n = self._current_index()
        if n is None:
            return
        nxt = max(0, min(n + step, len(self.rows) - 1))
        self._set_marks([n, nxt], True)
        self.anchor = nxt
        self._move(nxt)

    @plugin.command("mysync.job.markDown")
    def mark_down(self) -> None:
        self._extend(1)

    @plugin.command("mysync.job.markUp")
    def mark_up(self) -> None:
        self._extend(-1)

    @plugin.command("mysync.job.markAll")
    def mark_all(self) -> None:
        everything = all(i.path in self.marked for i in self.rows)
        self._set_marks(list(range(len(self.rows))), not everything)

    @plugin.command("mysync.job.unmark")
    def unmark(self) -> None:
        self.marked.clear()
        self._update_summary()

    # -- choosing actions ------------------------------------------------------------
    def _apply_many(self, choose: Callable[[Item], Action | None]) -> None:
        if self.cmp is None:
            return
        targets = self._targets()
        changes = [
            (i, a) for i in targets if (a := choose(i)) is not None and a != i.action
        ]
        if not changes:
            return
        if len(targets) == 1 and len(changes) == 1:
            self._apply(*changes[0])
            return
        done, failed = self.cmp.set_actions(changes)
        self._refresh()
        self.rcp.status(
            f"{done} item(s) changed" + (f", {failed} not possible" if failed else ""),
            "error" if failed else "normal",
        )

    def _apply(self, item: Item, action: Action) -> None:
        if self.cmp is None:
            return
        if not self.cmp.set_action(item, action):
            self.rcp.status(
                f"'{action.value}' is not possible for {item.path}", "error"
            )
            return
        self._report_conflicts()
        if item.is_dir or item.move_src or item.move_dst:
            self._refresh()  # folders and renames affect other rows
        else:
            self._update_summary()

    @plugin.command("mysync.job.right")
    def to_right(self) -> None:
        self._apply_many(lambda i: toward(i, "right"))

    @plugin.command("mysync.job.left")
    def to_left(self) -> None:
        self._apply_many(lambda i: toward(i, "left"))

    def _set_action(self, name: str) -> None:
        self._apply_many(
            lambda i: i.default_action if name == "DEFAULT" else Action[name]
        )

    @plugin.command("mysync.job.default")
    def set_default(self) -> None:
        self._set_action("DEFAULT")

    @plugin.command("mysync.job.skip")
    def set_skip(self) -> None:
        self._set_action("SKIP")

    @plugin.command("mysync.job.deleteLeft")
    def delete_left(self) -> None:
        self._set_action("DELETE_L")

    @plugin.command("mysync.job.deleteRight")
    def delete_right(self) -> None:
        self._set_action("DELETE_R")

    @plugin.command("mysync.job.ignore")
    def ignore(self) -> None:
        """Leave the marked rows (or the current one) out of this round, or take them
        back; a folder takes its content along. Comparing again resets this."""
        if self.cmp is None:
            return
        targets = self._targets()
        if not targets:
            return
        on = not all(i.ignored for i in targets)
        cursor = self._current_index() or 0
        changed, failed = self.cmp.set_ignored(targets, on)
        self.marked -= {i.path for i in targets}
        self._refresh()
        if self.rows:
            self._move(min(cursor + (not on), len(self.rows) - 1))
        if failed:
            self.rcp.status(
                f"{failed} item(s) belong to a renamed folder: ignore the folder instead",
                "error",
            )
        elif changed > 1 or len(targets) > 1:
            self.rcp.status(
                f"{changed} item(s) {'ignored this time' if on else 'taken back'}"
            )

    @plugin.command("mysync.job.cycle")
    def cycle(self) -> None:
        item = self._current()
        if item is None:
            return
        options = item.allowed_actions()
        if item.default_action not in options:
            options.insert(0, item.default_action)
        nxt = (
            options[(options.index(item.action) + 1) % len(options)]
            if item.action in options
            else options[0]
        )
        self._apply(item, nxt)

    @plugin.command("mysync.job.newer")
    def resolve_newer(self) -> None:
        if self.cmp is None:
            return
        n = 0
        for item in self.cmp.items:
            if (
                item.conflict
                and item.action == Action.SKIP
                and not item.locked
                and not item.ignored
            ):
                if item.left and item.right:
                    act = (
                        Action.COPY_LR
                        if item.left.mtime >= item.right.mtime
                        else Action.COPY_RL
                    )
                else:
                    act = Action.COPY_LR if item.left else Action.COPY_RL
                self.cmp.set_action(item, act)
                n += 1
        self._refresh()
        self.rcp.status(f"{n} conflict(s) resolved: newer side wins")

    # -- comparing two copies ------------------------------------------------------------
    @plugin.command("mysync.job.diff")
    def diff(self) -> None:
        """The two copies of the file side by side (vimdiff, or the linked editor). A
        copy not on this machine is fetched first; a missing one is an empty file."""
        item = self._current()
        if item is None or item.is_dir:
            self.rcp.status("Compare: put the cursor on a file", "error")
            return
        self._close_dialog()
        entries = {"left": item.left, "right": item.right}
        for side, e in entries.items():
            if e is not None and e.kind != "f":
                self.rcp.status(f"Compare: the {side} copy is not a file", "error")
                return
            if e is not None and e.size > COMPARE_LIMIT:
                self.rcp.status(
                    f"Compare: the {side} copy is {fmt.size(e.size)}", "error"
                )
                return
        name = item.path.rsplit("/", 1)[-1]
        job = self.job
        if self.tmp is None:
            self.tmp = Path(tempfile.mkdtemp(prefix=f"myrcp-mysync-{job.name}-"))
        self.fetches += 1
        where = self.tmp / str(self.fetches)

        def local(side: str) -> Path | None:
            loc = job.left if side == "left" else job.right
            if loc.scheme == "local" and entries[side] is not None:
                return Path(loc.path) / item.path
            return None

        def work(_j: Job) -> dict[str, Path]:
            out = {}
            for side in ("left", "right"):
                if (p := local(side)) is not None:
                    out[side] = p
                    continue
                dst = where / side / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                if entries[side] is None:
                    dst.write_bytes(b"")  # not there: compared with nothing
                else:
                    loc = job.left if side == "left" else job.right
                    with (
                        open_backend(loc) as b,
                        b.open_read(item.path) as src,
                        dst.open("wb") as f,
                    ):
                        shutil.copyfileobj(src, f)
                out[side] = dst
            return out

        def done(j: Job) -> None:
            if not j.ok:
                self.rcp.status(f"Compare: {j.error or 'cancelled'}", "error")
                return
            self.rcp.compare(j.result["left"], j.result["right"], title=f"⇆ {name}")

        self.rcp.run_job(f"Fetch {name} to compare", work, on_done=done)

    # -- dialogs -----------------------------------------------------------------------
    def _close_dialog(self) -> None:
        self.dialog = None
        self.detail = None

    @plugin.command("mysync.job.details")
    def details(self) -> None:
        item = self._current()
        if item is not None:
            self.detail = item
            self.dialog = "details"

    def _details_dialog(self, i: Item) -> ui.Node:
        lines: list[ui.Node] = [
            ui.text(i.path + ("/" if i.is_dir else ""), "bold"),
            ui.text(""),
            ui.text(
                f"Left:   {fmt.entry(i.left) or '(missing)'}   [{i.lchange.value}]"
            ),
            ui.text(
                f"Right:  {fmt.entry(i.right) or '(missing)'}   [{i.rchange.value}]"
            ),
            ui.text(
                [
                    f"Status: {fmt.CATEGORY_LABEL[i.category]}",
                    ("  CONFLICT", "warning") if i.conflict else "",
                ]
            ),
            ui.text(f"Reason: {i.reason or '-'}", wrap=True),
        ]
        if i.move_src:
            lines.append(ui.text(f"Rename: {i.move_src} → {i.path}"))
        if i.move_dst:
            lines.append(ui.text(f"Can be renamed to: {i.move_dst}"))
        if i.via_move:
            lines.append(ui.text("Handled by renaming its folder."))
        lines.append(
            ui.text(
                f"Action: {describe(i)}"
                + (" (default)" if i.action == i.default_action else "")
            )
        )
        if i.error:
            lines.append(ui.text(f"Error: {i.error}", "error", wrap=True))
        buttons = []
        if not i.is_dir:
            buttons.append(ui.button("diff", "Compare", on_press=self.diff))
        if not i.ignored:
            for act in i.allowed_actions():
                label = ACTION_TEXT[act].strip() + " " + act.value
                buttons.append(
                    ui.button(f"act-{act.name}", label, on_press=self._choose(i, act))
                )
            buttons.append(
                ui.button(
                    "default", "Default", on_press=self._choose(i, i.default_action)
                )
            )
        buttons.append(ui.button("close", "Close", on_press=self._close_dialog))
        return ui.vbox(*lines, ui.text(""), ui.hbox(*buttons))

    def _choose(self, item: Item, action: Action) -> Callable[[], None]:
        def choose() -> None:
            self._close_dialog()
            self._apply(item, action)

        return choose

    @plugin.command("mysync.job.edit")
    def edit(self) -> None:
        if self.job.file is not None:
            self.rcp.open_in_editor(self.job.file, on_close=self._edited)

    def _edited(self, waited: bool) -> None:
        if waited:
            self.compare()  # reloads the job file first
        else:
            self.rcp.status("Compare again (c) after saving the job file")

    @plugin.command("mysync.job.selection")
    def selection(self) -> None:
        if not self.job.selective:
            self.rcp.status(
                'Selective sync is off for this job: add  selective = "left"  (the side that keeps only '
                "a selection) to the job file.",
                "error",
            )
            return
        self.rcp.open_view("mysync.select", self.job.name)

    # -- synchronizing ---------------------------------------------------------------
    @plugin.command("mysync.job.sync")
    def sync(self) -> None:
        if self.cmp is None or self.comparing or (self.run and self.run.running):
            return
        if not self.cmp.pending():
            self.rcp.status("Nothing to synchronize")
            return
        self.dialog = "confirm"

    def _confirm_dialog(self) -> ui.Node:
        cmp = self.cmp
        assert cmp is not None
        c = cmp.counts()
        rows: list[tuple[str, Any]] = [
            ("Copy to the right", c[Action.COPY_LR.value]),
            ("Copy to the left", c[Action.COPY_RL.value]),
            ("Renames (no transfer)", c[Action.MOVE_R.value] + c[Action.MOVE_L.value]),
            (
                "Time/permission updates",
                c[Action.ATTR_LR.value] + c[Action.ATTR_RL.value],
            ),
            *([("Name spelling fixes", c["names"])] if c["names"] else []),
            ("Delete on the left", c[Action.DELETE_L.value]),
            ("Delete on the right", c[Action.DELETE_R.value]),
            *(
                [
                    (
                        f"Free on the {self.job.selective} (verified)",
                        (
                            f"{c[Action.EVICT_L.value] + c[Action.EVICT_R.value]} + uploaded ones, "
                            f"{fmt.size(cmp.freed_bytes())}"
                        ),
                    )
                ]
                if self.job.selective
                else []
            ),
            ("Unresolved conflicts (skipped)", c["conflict"]),
            *([("Ignored this time", c["ignored"])] if c["ignored"] else []),
            ("Data to transfer", fmt.size(cmp.transfer_bytes())),
            (
                "Deleted/overwritten files",
                "moved to trash"
                if self.job.deletion == "trash"
                else "deleted permanently",
            ),
        ]
        body = [ui.text(f"{label + ':':<32}{value}") for label, value in rows]
        problem = cmp.safety_problem()
        if problem:
            body += [
                ui.text(""),
                ui.text(f"⚠ {problem}.", "error", wrap=True),
                ui.text("Check the plan carefully before continuing."),
            ]
        return ui.vbox(
            *body,
            ui.text(""),
            ui.hbox(
                ui.button(
                    "ok",
                    "Synchronize anyway" if problem else "Synchronize",
                    on_press=lambda: self._start_sync(force=bool(problem)),
                ),
                ui.button("cancel", "Cancel", on_press=self._close_dialog),
            ),
        )

    def _start_sync(self, force: bool) -> None:
        assert self.cmp is not None
        self.run = _SyncRun(self.cmp, force)
        self.run.reporter = _Reporter(self, self.run)
        self.dialog = "sync"
        self._launch(retry=False)

    def _launch(self, retry: bool) -> None:
        run = self.run
        assert run is not None and run.reporter is not None
        rep = run.reporter
        job = self.job
        cmp = run.cmp

        def work(j: Job) -> SyncResult | None:
            rep.job = j
            rep.cancel_event = (
                j.cancel_event
            )  # the workbench's "cancel" stops the run too
            log = RunLog(job.name)
            log.write(
                f"sync {job.name}: {job.left.url} <-> {job.right.url} ({job.mode})"
                + (" - retrying failed items" if retry else "")
            )
            rep.runlog = log
            result: SyncResult | None = None
            error = ""
            try:
                with open_backend(job.left) as lb, open_backend(job.right) as rb:
                    if run.syncer is None:
                        problem = cmp.safety_problem()
                        if problem and not run.force:
                            raise SyncError(problem)
                        run.syncer = Synchronizer(cmp, lb, rb, rep)
                        result = run.syncer.run()
                    else:
                        result = run.syncer.retry_failed(lb, rb)
            except (SyncError, OSError) as e:
                error = str(e)
                rep.log(f"ERROR {error}")
            finally:
                conflicts = cmp.counts()["conflict"]
                hooks.post_sync(
                    job,
                    hooks.status_of(result, conflicts),
                    result,
                    conflicts,
                    str(log.path),
                    error,
                    rep.log,
                )
                log.close()
                rep.runlog = None
            run.result, run.error, run.log_path = result, error, str(log.path)
            return result

        run.running = True
        run.result = None
        self.rcp.run_job(f"Sync {job.name}", work, on_done=self._synced)

    def _synced(self, j: Job) -> None:
        run = self.run
        if run is None:
            return
        run.running = False
        if j.error is not None and not run.error:
            run.error = f"{type(j.error).__name__}: {j.error}"
        self._refresh()

    def _sync_dialog(self, run: _SyncRun) -> ui.Node:
        elapsed = max(time.monotonic() - run.started, 0.001)
        with run.lock:
            lines = list(run.lines[-500:])
            items_done, bytes_done = run.items_done, run.bytes_done
        stats = (
            f"{items_done}/{run.total_items} items   {fmt.size(bytes_done)} / {fmt.size(run.total_bytes)}   "
            f"{fmt.size(int(bytes_done / elapsed))}/s   {fmt.duration(elapsed)}"
        )
        status: ui.Node
        result = run.result
        if run.running:
            status = ui.text(run.current or "starting…")
        elif result is None:
            status = ui.text(
                f"Synchronization failed: {run.error}\nLog: {run.log_path}",
                "error",
                wrap=True,
            )
        else:
            bad = result.failed or not result.state_saved
            summary = (
                f"{'Cancelled' if result.cancelled else 'Finished'}: {result.done} done, "
                f"{result.failed} failed, {result.skipped} skipped, "
                + (
                    f"{result.changed} changed meanwhile (compare again), "
                    if result.changed
                    else ""
                )
                + (f"{fmt.size(result.freed)} freed, " if result.freed else "")
                + f"{fmt.size(result.bytes)} in {fmt.duration(result.seconds)}"
            )
            extra = "".join(f"\n{err}" for err in result.errors if "state" in err)
            status = ui.text(
                f"{summary}{extra}\nLog: {run.log_path}",
                "error" if bad else "ok",
                wrap=True,
            )
        bytes_value = (
            1.0
            if result and not result.cancelled
            else bytes_done / max(run.total_bytes, 1)
        )
        buttons = []
        if run.running:
            buttons.append(ui.button("cancel", "Cancel", on_press=self._sync_cancel))
        else:
            if result and result.failed and run.syncer:
                buttons.append(
                    ui.button(
                        "retry",
                        f"Retry failed ({result.failed})",
                        on_press=self._sync_retry,
                    )
                )
            buttons.append(ui.button("close", "Close", on_press=self._sync_close))
        return ui.vbox(
            ui.progress(items_done / max(run.total_items, 1), "Items"),
            ui.progress(bytes_value, "Data "),
            status,
            ui.text(stats, "dim"),
            ui.log(
                "sync-log",
                [cell(m, "error") if m.startswith("ERROR") else m for m in lines],
                height=12,
            ),
            ui.hbox(*buttons),
        )

    def _sync_cancel(self) -> None:
        run = self.run
        if run and run.running and run.reporter:
            run.reporter.cancel_event.set()
            run.current = "cancelling after the current item…"

    def _sync_escape(self) -> None:
        if self.run and self.run.running:
            self._sync_cancel()
        else:
            self._sync_close()

    def _sync_retry(self) -> None:
        run = self.run
        if run is None or run.syncer is None:
            return
        failed = run.syncer.failed_items()
        if not failed:
            return
        run.reset(len(failed), sum(i.transfer_size for i in failed))
        run.current = f"retrying {len(failed)} failed item(s)…"
        self._launch(retry=True)

    def _sync_close(self) -> None:
        run = self.run
        self.dialog = None
        self._refresh()
        if (
            run is not None
            and run.result is not None
            and not run.result.failed
            and not run.result.cancelled
        ):
            self.compare()


# ---------------------------------------------------------------------------------
# the selection of a selective-sync job


@plugin.view("mysync.select")
class SelectView(View):
    """Choose the folders and files the selective side keeps."""

    def setup(self) -> None:
        from mysync import selection as selection_mod
        from mysync.selection import build_index, plan_totals

        self.selection_mod = selection_mod
        self.build_index, self.plan_totals = build_index, plan_totals
        self.job = config_mod.find_job(str(self.arg))
        if not self.job.selective:
            raise ValueError(
                f'{self.job.name}: selective sync is off (add  selective = "left"  to the job)'
            )
        self.partial = self.job.selective
        self.full_side = "right" if self.partial == "left" else "left"
        self.sel = selection_mod.load(self.job)
        self.saved_rules = dict(self.sel.rules)
        self.nodes: dict[str, Any] = {}
        self.full: dict[str, Any] = {}
        self.local: dict[str, Any] = {}
        self.expanded: set[str] = {""}
        self.visible: list[tuple[str, int]] = []
        self.where: dict[str, int] = {}
        self.cursor = ui.CursorRequest()
        self.message: Spans = [("Scanning…", "dim")]
        self.confirm_discard = False
        self._scan()

    def title(self) -> str:
        return f"☑ {self.job.name}"

    # -- scanning --------------------------------------------------------------------
    def _scan(self) -> None:
        from mysync.compare import builtin_excludes
        from mysync.filters import PathFilter

        job = self.job

        def work(j: Job) -> dict[str, Any]:
            flt = PathFilter(job.include, job.exclude + builtin_excludes(job))
            scans = {}
            with open_backend(job.left) as lb, open_backend(job.right) as rb:
                for side, backend in (("left", lb), ("right", rb)):
                    self.message = [(f"Scanning the {side}…", "dim")]
                    j.progress(0.5 if side == "right" else 0, f"the {side}")
                    self.invalidate()
                    scans[side] = (
                        backend.scan(flt.excluded, job.symlinks)
                        if backend.root_exists()
                        else {}
                    )
            return scans

        self.rcp.run_job(f"Scan {job.name}", work, on_done=self._scanned)

    def _scanned(self, j: Job) -> None:
        if not j.ok:
            self.message = [("Scanning failed: ", "error"), str(j.error or "cancelled")]
            return
        self.full, self.local = j.result[self.full_side], j.result[self.partial]
        self.nodes = self.build_index(self.full, self.local)
        self._flatten()

    # -- the tree ----------------------------------------------------------------------
    def _flatten(self) -> None:
        """The rows shown: every node whose parents are all expanded, depth first."""
        self.visible = []
        stack = [("", 0)]
        while stack:
            path, depth = stack.pop()
            self.visible.append((path, depth))
            if path in self.expanded:
                stack.extend(
                    (c, depth + 1) for c in reversed(self.nodes[path].children)
                )
        self.where = {path: n for n, (path, _) in enumerate(self.visible)}

    def _row(self, i: int) -> ui.Node:
        path, depth = self.visible[i]
        data = self.nodes[path]
        name = (
            "/" if not path else path.rsplit("/", 1)[-1] + ("/" if data.is_dir else "")
        )
        state = self.sel.state(path)
        check = {self.selection_mod.ALL: "all", self.selection_mod.NONE: "none"}.get(
            state, "some"
        )
        size = data.full_size if data.full else data.local_size
        info = []
        if data.is_dir or size:
            info.append(fmt.size(size))
        if not data.full:
            info.append("only here: uploaded at next sync")
        elif (
            data.local
            and data.is_dir
            and data.local_size
            and data.local_size != data.full_size
        ):
            info.append(f"{fmt.size(data.local_size)} local")
        elif data.local:
            info.append("local")
        return ui.tree_row(
            path,
            cell(name, "bold" if data.is_dir else "normal"),
            depth=depth,
            expandable=data.is_dir and bool(data.children),
            expanded=path in self.expanded,
            check=check,
            detail=" · ".join(info),
        )

    def _expand(self, key: str) -> None:
        node = self.nodes.get(key)
        if node is not None and node.is_dir and node.children:
            self.expanded.add(key)
            self._flatten()

    def _collapse(self, key: str) -> None:
        if key in self.expanded:
            self.expanded.discard(key)
            self._flatten()
        elif key:  # closed already: go to the parent
            parent = key.rsplit("/", 1)[0] if "/" in key else ""
            self.cursor.move(self.where.get(parent, 0))

    def _activate(self, key: str) -> None:
        (self._collapse if key in self.expanded else self._expand)(key)

    def _toggle(self, key: str) -> None:
        if key in self.nodes:
            self.sel.toggle(key)

    @plugin.command("mysync.select.everything")
    def everything(self) -> None:
        self.sel.set("", True)

    @plugin.command("mysync.select.expandAll")
    def expand_all(self) -> None:
        self.expanded = {p for p, n in self.nodes.items() if n.is_dir and n.children}
        self._flatten()

    # -- rendering ---------------------------------------------------------------------
    def _changed(self) -> bool:
        return self.sel.rules != self.saved_rules

    def _summary(self) -> Spans:
        if not self.nodes:
            return self.message
        selected, total, download, free = self.plan_totals(
            self.nodes, self.full, self.local, self.sel
        )
        out: Spans = [
            "Selected ",
            (fmt.size(selected), "bold"),
            f" of {fmt.size(total)} on the {self.full_side}.   ",
        ]
        out += ["\nNext sync: ", (f"download {fmt.size(download)}", "accent"), ", "]
        out += [(f"free {fmt.size(free)}", "warning"), f" on the {self.partial}."]
        if self._changed():
            out += ["   ", ("Not saved.", "bold")]
        return out

    def render(self) -> ui.Node:
        loc = getattr(self.job, self.partial)
        other = getattr(self.job, self.full_side)
        base = ui.vbox(
            ui.text(
                [
                    (self.job.name, "bold"),
                    " – selective sync: choose what ",
                    (loc.url, "bold"),
                    " keeps.",
                ]
            ),
            ui.text(
                f"Everything stays on {other.url}. Unselected items are not downloaded; local copies of them "
                "are uploaded if needed, verified and then removed.",
                "dim",
                wrap=True,
            ),
            ui.tree(
                "tree",
                ui.lazy_rows(len(self.visible), self._row),
                cursor=self.cursor,
                autofocus=True,
                empty="Scanning…",
                on_expand=self._expand,
                on_collapse=self._collapse,
                on_toggle=self._toggle,
                on_activate=self._activate,
            ),
            ui.text(self._summary()),
            ui.hbox(
                ui.button("save", "Save", on_press=self.save),
                ui.button("everything", "Select all", on_press=self.everything),
                ui.button("expand", "Expand all", on_press=self.expand_all),
                ui.button("back", "Back", on_press=self.back),
            ),
            hints(
                "space",
                "select",
                "→/←",
                "open/close",
                "a",
                "select all",
                "*",
                "expand all",
                "w",
                "save",
                "q",
                "back",
            ),
        )
        if not self.confirm_discard:
            return base
        return ui.modal(
            "discard",
            base,
            ui.vbox(
                ui.text("The selection has not been saved."),
                ui.text(""),
                ui.hbox(
                    ui.button("discard", "Discard", on_press=self.close),
                    ui.button("keep", "Keep editing", on_press=self._keep),
                ),
            ),
            title="Discard changes?",
            on_dismiss=self._keep,
            width=50,
        )

    def _keep(self) -> None:
        self.confirm_discard = False

    @plugin.command("mysync.select.save")
    def save(self) -> None:
        if not self._changed():
            self.close()
            return
        path = self.selection_mod.save(self.job, self.sel)
        self.saved_rules = dict(self.sel.rules)
        self.rcp.status(f"Saved {path}; comparing again", "ok")
        job_view = _JOB_VIEWS.get(self.job.name)
        if job_view is not None:
            job_view.compare()  # review the resulting plan
        self.rcp.open_view("mysync.job", self.job.name)
        self.close()

    @plugin.command("mysync.select.back")
    def back(self) -> None:
        if self._changed():
            self.confirm_discard = True
        else:
            self.close()
