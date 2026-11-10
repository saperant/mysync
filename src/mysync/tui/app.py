"""Textual user interface.  Everything is usable with the mouse as well as the keyboard."""

from __future__ import annotations

import subprocess
import time

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, Checkbox, DataTable, Footer, Header, Input, Label, ProgressBar, RichLog, Select, Static,
)

from mysync import config as config_mod
from mysync import fmt, hooks, paths
from mysync import state as state_mod
from mysync.backends import open_backend
from mysync.cli import editor_command
from mysync.compare import Comparison, Item, compare, toward
from mysync.model import Action, Category, SyncError
from mysync.runlog import RunLog
from mysync.sync import Reporter, SyncResult, Synchronizer, describe

ACTION_STYLE = {
    Action.NONE: "dim",
    Action.COPY_LR: "bold green",
    Action.COPY_RL: "bold cyan",
    Action.MOVE_R: "bold magenta",
    Action.MOVE_L: "bold magenta",
    Action.ATTR_LR: "bold bright_blue",
    Action.ATTR_RL: "bold bright_blue",
    Action.DELETE_L: "bold red",
    Action.DELETE_R: "bold red",
    Action.EVICT_L: "bold bright_yellow",
    Action.EVICT_R: "bold bright_yellow",
    Action.SKIP: "yellow",
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
    Category.LEFT_ONLY: "green",
    Category.RIGHT_ONLY: "cyan",
    Category.LEFT_NEWER: "green",
    Category.RIGHT_NEWER: "cyan",
    Category.DIFFERENT: "magenta",
    Category.ATTRIBUTES: "bright_blue",
    Category.CONFLICT: "bold yellow",
}


def _action_cell(item: Item) -> Text:
    if item.ignored:
        return Text("  –  ", style="dim")
    if item.status == "done":
        return Text("  ✔  ", style="bold green")
    if item.status == "failed":
        return Text("  ✘  ", style="bold red reverse")
    if item.status == "changed":
        return Text("  ≠  ", style="bold yellow reverse")
    if item.status == "kept":
        return Text("  ⏏  ", style="dim")
    if item.moved_away:
        return Text("  ↳  ", style="dim magenta")
    if item.via_move:
        return Text("  ⋯  ", style="dim magenta")
    style = ACTION_STYLE[item.action]
    if item.conflict and item.action == Action.SKIP:
        style = "bold yellow reverse"
    if item.action == Action.NONE and item.name_fix:  # only the spelling of the name changes
        return Text(" Aa▶ " if item.name_fix == "right" else " ◀Aa ", style="magenta")
    return Text(ACTION_TEXT[item.action], style=style)


def _entry_cell(item: Item, side: str) -> Text:
    entry = item.left if side == "left" else item.right
    covered = item.covered and item.action == (Action.DELETE_L if side == "left" else Action.DELETE_R)
    text = Text(fmt.entry(entry), style="dim" if item.ignored else CATEGORY_STYLE[item.category])
    if covered:
        text.stylize("strike")
    return text


def _path_cell(item: Item, marked: bool = False) -> Text:
    name = item.path + ("/" if item.is_dir else "")
    if item.ignored:
        return Text(name, style="dim italic" + (" reverse" if marked else ""))
    if item.moved_away:
        return Text(name, style="dim strike" + (" reverse" if marked else ""))
    style = "bold" if item.is_dir else ""
    if item.conflict:
        style += " yellow"
    if marked:
        style += " reverse"
    return Text(name, style=style.strip())


def _reason_cell(item: Item) -> Text:
    if item.error:
        return Text(item.error, style="red")
    if item.held:
        return Text("kept: holds ignored items", style="dim italic")
    return Text(item.reason, style="dim")


def _mark_cell(marked: bool) -> Text:
    return Text("■" if marked else "·", style="bold yellow" if marked else "dim")


# --------------------------------------------------------------------------
# dialogs


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, title: str, body: str, ok_label: str = "OK", danger: bool = False):
        super().__init__()
        self._title, self._body, self._ok, self._danger = title, body, ok_label, danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self._title, classes="dialog-title")
            yield Static(self._body, classes="dialog-body")
            with Horizontal(classes="buttons"):
                yield Button(self._ok, id="ok", variant="error" if self._danger else "primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#ok")
    def _ok_pressed(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self) -> None:
        self.dismiss(False)


class MessageScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,enter", "dismiss", "Close")]

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self._title, classes="dialog-title")
            with VerticalScroll(classes="dialog-scroll"):
                yield Static(self._body, classes="dialog-body")
            with Horizontal(classes="buttons"):
                yield Button("Close", id="close", variant="primary")

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss()


class NewJobScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "dismiss(None)", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("New synchronization job", classes="dialog-title")
            yield Label("Name")
            yield Input(placeholder="photos", id="name")
            yield Label("Left location")
            yield Input(placeholder="~/Pictures", id="left")
            yield Label("Right location (path, sftp://host/path, ssh://host/path or rclone:remote:path)")
            yield Input(placeholder="sftp://server/srv/photos", id="right")
            yield Label("Mode")
            yield Select([(m, m) for m in config_mod.MODES], value="two-way", allow_blank=False, id="mode")
            yield Label("", id="error", classes="error")
            with Horizontal(classes="buttons"):
                yield Button("Create", id="create", variant="primary")
                yield Button("Create && edit", id="create-edit")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#create, #create-edit")
    def _create(self, event: Button.Pressed) -> None:
        name = self.query_one("#name", Input).value.strip()
        left = self.query_one("#left", Input).value.strip()
        right = self.query_one("#right", Input).value.strip()
        mode = self.query_one("#mode", Select).value
        try:
            if not (name and left and right):
                raise config_mod.ConfigError("name, left and right are required")
            config_mod.job_from_dict({"left": left, "right": right, "mode": mode}, name)
            path = config_mod.write_template(name, left, right)
            if mode != "two-way":
                path.write_text(path.read_text().replace('mode = "two-way"', f'mode = "{mode}"', 1))
        except config_mod.ConfigError as e:
            self.query_one("#error", Label).update(str(e))
            return
        self.dismiss(f"{'edit:' if event.button.id == 'create-edit' else ''}{path}")


class ItemScreen(ModalScreen[Action | None]):
    """Details of one item, with buttons to choose its action."""

    BINDINGS = [Binding("escape", "dismiss(None)", "Close")]

    def __init__(self, item: Item):
        super().__init__()
        self.item = item

    def compose(self) -> ComposeResult:
        i = self.item
        lines = [
            f"[b]{i.path}{'/' if i.is_dir else ''}[/b]",
            "",
            f"Left:   {fmt.entry(i.left) or '(missing)'}   [{i.lchange.value}]",
            f"Right:  {fmt.entry(i.right) or '(missing)'}   [{i.rchange.value}]",
            f"Status: {fmt.CATEGORY_LABEL[i.category]}" + ("  [yellow]CONFLICT[/]" if i.conflict else ""),
            f"Reason: {i.reason or '-'}",
            *([f"Rename: {i.move_src} → {i.path}"] if i.move_src else []),
            *([f"Can be renamed to: {i.move_dst}"] if i.move_dst else []),
            *(["Handled by renaming its folder."] if i.via_move else []),
            f"Action: {describe(i)}" + (" (default)" if i.action == i.default_action else ""),
        ]
        if i.error:
            lines.append(f"[red]Error: {i.error}[/]")
        with Vertical(classes="dialog wide"):
            yield Label("Item", classes="dialog-title")
            yield Static("\n".join(lines), classes="dialog-body")
            with Horizontal(classes="buttons"):
                for act in [] if i.ignored else i.allowed_actions():
                    yield Button(ACTION_TEXT[act].strip() + " " + act.value, id=f"act-{act.name}")
                if not i.ignored:
                    yield Button("Default", id="default")
                yield Button("Close", id="close", variant="primary")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("act-"):
            self.dismiss(Action[bid[4:]])
        elif bid == "default":
            self.dismiss(self.item.default_action)
        else:
            self.dismiss(None)


class _UiReporter(Reporter):
    def __init__(self, screen: "SyncScreen", log: RunLog):
        super().__init__()
        self.screen = screen
        self.runlog = log

    def log(self, message: str) -> None:
        self.runlog.write(message)
        self.screen.app.call_from_thread(self.screen.add_log, message)

    def item_start(self, item: Item, description: str) -> None:
        self.screen.app.call_from_thread(self.screen.set_current, f"{description}: {item.path}")

    def item_done(self, item: Item) -> None:
        self.screen.app.call_from_thread(self.screen.advance_item)

    def transferred(self, nbytes: int) -> None:
        self.screen.bytes_pending += nbytes


class SyncScreen(ModalScreen[SyncResult | None]):
    """Runs the synchronization and shows progress."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, cmp: Comparison, force: bool):
        super().__init__()
        self.cmp = cmp
        self.force = force
        self.total_items = len(self.cmp.pending())
        self.total_bytes = cmp.transfer_bytes()
        self.items_done = 0
        self.bytes_done = 0
        self.bytes_pending = 0
        self.started = time.monotonic()
        self.reporter: _UiReporter | None = None
        self.result: SyncResult | None = None
        self.syncer: Synchronizer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide tall"):
            yield Label(f"Synchronizing {self.cmp.job.name}", classes="dialog-title")
            yield Label("Items")
            yield ProgressBar(total=max(self.total_items, 1), id="items-bar", show_eta=False)
            yield Label("Data")
            yield ProgressBar(total=max(self.total_bytes, 1), id="bytes-bar", show_eta=True)
            yield Static("", id="current")
            yield Static("", id="stats")
            yield RichLog(id="log", wrap=False, markup=False, max_lines=5000)
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel", variant="error")
                yield Button("Retry failed", id="retry", variant="warning", disabled=True)
                yield Button("Close", id="close", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self.set_interval(0.25, self._tick)
        self._run()

    def _tick(self) -> None:
        if self.bytes_pending:
            self.bytes_done += self.bytes_pending
            self.bytes_pending = 0
            self.query_one("#bytes-bar", ProgressBar).update(progress=self.bytes_done)
        elapsed = max(time.monotonic() - self.started, 0.001)
        self.query_one("#stats", Static).update(
            f"{self.items_done}/{self.total_items} items   "
            f"{fmt.size(self.bytes_done)} / {fmt.size(self.total_bytes)}   "
            f"{fmt.size(int(self.bytes_done / elapsed))}/s   {fmt.duration(elapsed)}")

    def add_log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(
            Text(message, style="red" if message.startswith("ERROR") else ""))

    def set_current(self, text: str) -> None:
        self.query_one("#current", Static).update(text)

    def advance_item(self) -> None:
        self.items_done += 1
        self.query_one("#items-bar", ProgressBar).update(progress=self.items_done)

    @work(thread=True, exclusive=True, group="sync")
    def _run(self) -> None:
        job = self.cmp.job
        log = RunLog(job.name)
        log.write(f"sync {job.name}: {job.left.url} <-> {job.right.url} ({job.mode})"
                  + (" - retrying failed items" if self.syncer else ""))
        if self.reporter is None:
            self.reporter = _UiReporter(self, log)
        self.reporter.runlog = log
        result: SyncResult | None = None
        error = ""
        try:
            with open_backend(job.left) as lb, open_backend(job.right) as rb:
                if self.syncer is None:
                    problem = self.cmp.safety_problem()
                    if problem and not self.force:
                        raise SyncError(problem)
                    self.syncer = Synchronizer(self.cmp, lb, rb, self.reporter)
                    result = self.syncer.run()
                else:
                    result = self.syncer.retry_failed(lb, rb)
        except (SyncError, OSError) as e:
            error = str(e)
            self.reporter.log(f"ERROR {error}")
        finally:
            conflicts = self.cmp.counts()["conflict"]
            hooks.post_sync(job, hooks.status_of(result, conflicts), result, conflicts, str(log.path),
                            error, self.reporter.log)
            log.close()
        self.app.call_from_thread(self._finished, result, error, str(log.path))

    def _finished(self, result: SyncResult | None, error: str, log_path: str) -> None:
        self.result = result
        self._tick()
        if result is None:
            summary = f"[red]Synchronization failed: {error}[/]"
        else:
            if not result.cancelled:
                self.query_one("#bytes-bar", ProgressBar).update(progress=max(self.total_bytes, 1))
            colour = "red" if result.failed or not result.state_saved else "green"
            summary = (f"[{colour}]{'Cancelled' if result.cancelled else 'Finished'}: "
                       f"{result.done} done, {result.failed} failed, {result.skipped} skipped, "
                       + (f"{result.changed} changed meanwhile (compare again), " if result.changed else "")
                       + (f"{fmt.size(result.freed)} freed, " if result.freed else "")
                       + f"{fmt.size(result.bytes)} in {fmt.duration(result.seconds)}[/]")
            for err in result.errors:
                if "state" in err:
                    summary += f"\n[red]{err}[/]"
        self.query_one("#current", Static).update(f"{summary}\nLog: {log_path}")
        self.query_one("#cancel", Button).disabled = True
        retry = self.query_one("#retry", Button)
        retry.disabled = not (result and result.failed and self.syncer)
        retry.label = f"Retry failed ({result.failed})" if result and result.failed else "Retry failed"
        close = self.query_one("#close", Button)
        close.disabled = False
        close.focus()

    @on(Button.Pressed, "#retry")
    def _retry(self) -> None:
        if self.syncer is None:
            return
        failed = self.syncer.failed_items()
        if not failed:
            return
        self.result = None
        self.total_items = len(failed)
        self.total_bytes = sum(i.transfer_size for i in failed)
        self.items_done = self.bytes_done = self.bytes_pending = 0
        self.started = time.monotonic()
        self.query_one("#items-bar", ProgressBar).update(total=max(self.total_items, 1), progress=0)
        self.query_one("#bytes-bar", ProgressBar).update(total=max(self.total_bytes, 1), progress=0)
        self.query_one("#cancel", Button).disabled = False
        self.query_one("#retry", Button).disabled = True
        self.query_one("#close", Button).disabled = True
        self.set_current(f"retrying {len(failed)} failed item(s)…")
        self._run()

    def action_cancel(self) -> None:
        if self.result is not None or self.query_one("#close", Button).disabled is False:
            self.dismiss(self.result)
        elif self.reporter:
            self.reporter.cancel_event.set()
            self.set_current("cancelling after the current item…")

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#close")
    def _close(self) -> None:
        self.dismiss(self.result)


# --------------------------------------------------------------------------
# job screen


class PlanTable(DataTable):
    """DataTable that reports clicks on the mark and action columns.

    Clicking the mark column, or Ctrl+click anywhere, marks a row; Shift+click
    marks everything from the previously marked row.
    """

    MARK_COLUMN, ACTION_COLUMN = 0, 3

    class ActionClicked(Message):
        def __init__(self, row: int):
            super().__init__()
            self.row = row

    class MarkClicked(Message):
        def __init__(self, row: int, extend: bool):
            super().__init__()
            self.row = row
            self.extend = extend

    action_click_time = 0.0

    def on_click(self, event) -> None:
        meta = event.style.meta if event.style else {}
        row, column = meta.get("row", -1), meta.get("column")
        if row < 0:
            return
        if column == self.MARK_COLUMN or event.ctrl or event.shift:
            self.action_click_time = time.monotonic()  # not "open details"
            self.post_message(self.MarkClicked(row, event.shift))
        elif column == self.ACTION_COLUMN:
            self.action_click_time = time.monotonic()
            self.post_message(self.ActionClicked(row))


def _in_move(i: Item) -> bool:
    return i.via_move or i.moved_away


FILTERS = [
    ("f-copy-r", "──▶ to right", lambda i: not _in_move(i) and i.action in (Action.COPY_LR, Action.ATTR_LR)),
    ("f-copy-l", "◀── to left", lambda i: not _in_move(i) and i.action in (Action.COPY_RL, Action.ATTR_RL)),
    ("f-move", "mv renames", lambda i: (i.action.is_move and not i.via_move) or bool(i.name_fix)),
    ("f-delete", "✖ deletes/⏏ frees", lambda i: (i.action.is_delete and not i.moved_away)
     or i.action.is_evict or i.evict_after),
    ("f-conflict", "? conflicts", lambda i: i.action == Action.SKIP or i.conflict),
    ("f-equal", "= equal/details", lambda i: i.action == Action.NONE or _in_move(i)),
    ("f-ignored", "– ignored", lambda i: i.ignored),
]


class JobScreen(Screen):
    BINDINGS = [
        Binding("f5,c", "compare", "Compare"),
        Binding("f9,s", "sync", "Synchronize"),
        Binding("escape", "unmark_or_back", "Back", show=False),
        Binding("q", "back", "Back"),
        # priority: the table would use the arrow keys for horizontal movement
        Binding("right,greater_than_sign", "direction('right')", "→ to right", priority=True),
        Binding("left,less_than_sign", "direction('left')", "← to left", priority=True),
        Binding("m,insert", "mark", "Mark"),
        Binding("shift+down", "extend(1)", "Mark down", show=False, priority=True),
        Binding("shift+up", "extend(-1)", "Mark up", show=False, priority=True),
        Binding("ctrl+a", "mark_all", "Mark all", show=False),
        Binding("space", "ignore", "Ignore"),
        Binding("a", "cycle", "Cycle action"),
        Binding("i", "details", "Details"),
        Binding("d", "set_action('DEFAULT')", "Default", show=False),
        Binding("x", "set_action('SKIP')", "Skip", show=False),
        Binding("n", "resolve_newer", "Conflicts: newer wins"),
        Binding("e", "edit", "Edit job"),
        Binding("t", "select", "Selection"),
    ]

    def __init__(self, job: config_mod.JobConfig):
        super().__init__()
        self.job = job
        self.cmp: Comparison | None = None
        self.rows: list[Item] = []
        self.marked: set[str] = set()
        self.anchor: int | None = None  # last row marked, for Shift+click ranges

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._job_info(), id="job-info")
        with Horizontal(id="filters"):
            for fid, label, _ in FILTERS:
                yield Checkbox(label, value=fid not in ("f-equal", "f-ignored"), id=fid)
        yield PlanTable(id="plan", cursor_type="row", zebra_stripes=True)
        yield Static("Press Compare (F5) to scan both locations.", id="summary")
        with Horizontal(classes="buttons", id="job-buttons"):
            yield Button("Compare", id="compare", variant="primary")
            yield Button("Synchronize", id="sync", variant="success", disabled=True)
            yield Button("──▶", id="dir-right", classes="small",
                         tooltip="Make the right like the left (→); marked rows or the current one")
            yield Button("◀──", id="dir-left", classes="small",
                         tooltip="Make the left like the right (←); marked rows or the current one")
            yield Button("✖◀", id="act-DELETE_L", classes="small", tooltip="Delete on the left")
            yield Button("▶✖", id="act-DELETE_R", classes="small", tooltip="Delete on the right")
            yield Button("Skip", id="act-SKIP", classes="small")
            yield Button("Default", id="act-DEFAULT", classes="small")
            if self.job.selective:
                yield Button("Selection…", id="select", tooltip="Choose what the "
                             f"{self.job.selective} side keeps (selective sync)")
            yield Button("Back", id="back")
        yield Footer()

    def _job_info(self) -> str:
        j = self.job
        return (f"[b]{j.name}[/b]  [dim]{j.mode}, compare by {j.compare}, conflicts: {j.conflict}, "
                f"deletion: {j.deletion}"
                + (f", selective sync: the {j.selective} keeps a selection (T)" if j.selective else "")
                + "[/dim]\n"
                f"[green]L[/] {j.left.url}\n[cyan]R[/] {j.right.url}")

    def on_mount(self) -> None:
        self.title = f"mysync – {self.job.name}"
        table = self.query_one(PlanTable)
        table.add_column("", width=1, key="mark")
        table.add_column("Left", width=31, key="left")
        table.add_column("Path", key="path")
        table.add_column("Action", width=7, key="action")
        table.add_column("Right", width=31, key="right")
        table.add_column("Reason", key="reason")
        table.focus()
        self.action_compare()

    # -- comparing ---------------------------------------------------------
    def action_compare(self) -> None:
        if self.workers_running():
            return
        self.query_one("#compare", Button).disabled = True
        self.query_one("#sync", Button).disabled = True
        self._set_summary("Connecting…")
        self._compare_worker()

    def workers_running(self) -> bool:
        return any(w.group == "compare" and w.is_running for w in self.workers)

    def _set_summary(self, text: str) -> None:
        self.query_one("#summary", Static).update(text)

    @work(thread=True, exclusive=True, group="compare")
    def _compare_worker(self) -> None:
        def stage(msg: str) -> None:
            self.app.call_from_thread(self._set_summary, f"[b]Comparing:[/b] {msg}")
        try:
            with open_backend(self.job.left) as lb, open_backend(self.job.right) as rb:
                cmp = compare(self.job, lb, rb, stage)
        except (SyncError, OSError) as e:
            self.app.call_from_thread(self._compare_failed, str(e))
            return
        self.app.call_from_thread(self._compare_done, cmp)

    def _compare_failed(self, error: str) -> None:
        self.query_one("#compare", Button).disabled = False
        self._set_summary(f"[red]Comparison failed:[/red] {error}")

    def _compare_done(self, cmp: Comparison) -> None:
        self.cmp = cmp
        self.query_one("#compare", Button).disabled = False
        self._refresh_table()

    # -- table -------------------------------------------------------------
    def _filters(self):
        return [pred for fid, _, pred in FILTERS if self.query_one(f"#{fid}", Checkbox).value]

    def _refresh_table(self, keep_cursor: bool = True) -> None:
        table = self.query_one(PlanTable)
        old_row = table.cursor_row
        table.clear()
        self.rows = []
        if self.cmp is None:
            return
        preds = self._filters()
        show_ignored = self.query_one("#f-ignored", Checkbox).value
        for item in self.cmp.items:
            if item.ignored and not show_ignored:
                continue
            if not item.ignored and not any(p(item) for p in preds):
                continue
            self.rows.append(item)
            marked = item.path in self.marked
            table.add_row(_mark_cell(marked), _entry_cell(item, "left"), _path_cell(item, marked),
                          _action_cell(item),
                          _entry_cell(item, "right"), _reason_cell(item), key=item.path)
        if keep_cursor and self.rows:
            table.move_cursor(row=min(old_row, len(self.rows) - 1))
        self._update_summary()

    def _update_row(self, item: Item) -> None:
        table = self.query_one(PlanTable)
        try:
            marked = item.path in self.marked
            table.update_cell(item.path, "mark", _mark_cell(marked))
            table.update_cell(item.path, "path", _path_cell(item, marked))
            table.update_cell(item.path, "action", _action_cell(item))
            table.update_cell(item.path, "left", _entry_cell(item, "left"))
            table.update_cell(item.path, "right", _entry_cell(item, "right"))
            table.update_cell(item.path, "reason", _reason_cell(item))
        except Exception:  # noqa: BLE001 - row filtered out
            pass

    def _update_summary(self) -> None:
        if self.cmp is None:
            return
        c = self.cmp.counts()
        pending = len(self.cmp.pending())
        text = (f"[green]──▶ {c[Action.COPY_LR.value]}[/]   [cyan]◀── {c[Action.COPY_RL.value]}[/]   "
                f"[magenta]mv {c[Action.MOVE_R.value] + c[Action.MOVE_L.value]}[/]   "
                f"[bright_blue]attr {c[Action.ATTR_LR.value] + c[Action.ATTR_RL.value]}[/]   "
                + (f"[magenta]Aa {c['names']}[/]   " if c["names"] else "") +
                f"[red]✖◀ {c[Action.DELETE_L.value]}   ▶✖ {c[Action.DELETE_R.value]}[/]   "
                f"[yellow]conflicts {c['conflict']}[/]   skip {c[Action.SKIP.value]}   "
                f"transfer {fmt.size(self.cmp.transfer_bytes())}   "
                + (f"[bright_yellow]⏏ free {fmt.size(self.cmp.freed_bytes())}[/]   " if self.job.selective else "")
                + (f"[dim]– {c['ignored']} ignored[/]   " if c["ignored"] else "")
                + (f"[bold yellow]■ {len(self._marked_rows())} marked[/]   " if self.marked else "")
                + f"[dim]({len(self.rows)} shown of {len(self.cmp.items)}; {self.cmp.state_message})[/]")
        problem = self.cmp.safety_problem()
        if problem:
            text += f"\n[bold red]⚠ {problem}[/]"
        if self.cmp.warnings:
            text += f"\n[yellow]⚠ {self.cmp.warnings[0]}" + (
                f" (and {len(self.cmp.warnings) - 1} more)" if len(self.cmp.warnings) > 1 else "") + "[/]"
        self._set_summary(text)
        self.query_one("#sync", Button).disabled = pending == 0

    @on(Checkbox.Changed)
    def _filter_changed(self) -> None:
        self._refresh_table(keep_cursor=False)

    def _current(self) -> Item | None:
        table = self.query_one(PlanTable)
        if not self.rows or table.cursor_row < 0 or table.cursor_row >= len(self.rows):
            return None
        return self.rows[table.cursor_row]

    # -- marking -------------------------------------------------------------
    def _marked_rows(self) -> list[Item]:
        return [i for i in self.rows if i.path in self.marked]

    def _targets(self) -> list[Item]:
        """Marked rows that are shown, or else the current row."""
        marked = self._marked_rows()
        if marked:
            return marked
        item = self._current()
        return [item] if item else []

    def _set_marks(self, rows: list[int], value: bool | None = None) -> None:
        """Mark (True), unmark (False) or toggle (None) rows by index."""
        for n in rows:
            if not 0 <= n < len(self.rows):
                continue
            item = self.rows[n]
            on = (item.path not in self.marked) if value is None else value
            (self.marked.add if on else self.marked.discard)(item.path)
            self._update_row(item)
        self._update_summary()

    def action_mark(self) -> None:
        table = self.query_one(PlanTable)
        if not self.rows:
            return
        self._set_marks([table.cursor_row])
        self.anchor = table.cursor_row
        table.move_cursor(row=min(table.cursor_row + 1, len(self.rows) - 1))

    def action_extend(self, step: int) -> None:
        table = self.query_one(PlanTable)
        if not self.rows:
            return
        row = table.cursor_row
        nxt = max(0, min(row + step, len(self.rows) - 1))
        self._set_marks([row, nxt], True)
        self.anchor = nxt
        table.move_cursor(row=nxt)

    def action_mark_all(self) -> None:
        everything = all(i.path in self.marked for i in self.rows)
        self._set_marks(list(range(len(self.rows))), not everything)

    def action_unmark_or_back(self) -> None:
        if self.marked:
            self.marked.clear()
            self._refresh_table()
        else:
            self.action_back()

    @on(PlanTable.MarkClicked)
    def _mark_clicked(self, event: PlanTable.MarkClicked) -> None:
        self.query_one(PlanTable).move_cursor(row=event.row)
        if event.extend and self.anchor is not None:
            lo, hi = sorted((self.anchor, event.row))
            self._set_marks(list(range(lo, hi + 1)), True)
        else:
            self._set_marks([event.row])
        self.anchor = event.row

    # -- choosing actions ------------------------------------------------------
    def _apply_many(self, choose) -> None:
        """Apply ``choose(item) -> Action | None`` to the targets."""
        if self.cmp is None:
            return
        targets = self._targets()
        changes = [(i, a) for i in targets if (a := choose(i)) is not None and a != i.action]
        if not changes:
            return
        if len(targets) == 1 and len(changes) == 1:
            self._apply(*changes[0])
            return
        done, failed = self.cmp.set_actions(changes)
        self._refresh_table()
        self.notify(f"{done} item(s) changed" + (f", {failed} not possible" if failed else ""),
                    severity="warning" if failed else "information")

    def action_direction(self, side: str) -> None:
        self._apply_many(lambda i: toward(i, side))

    def _apply(self, item: Item, action: Action) -> None:
        if self.cmp is None:
            return
        if not self.cmp.set_action(item, action):
            self.notify(f"'{action.value}' is not possible for {item.path}", severity="warning")
            return
        if item.is_dir or item.move_src or item.move_dst:
            self._refresh_table()  # folders and renames affect other rows
        else:
            self._update_row(item)
            self._update_summary()

    def action_set_action(self, name: str) -> None:
        self._apply_many(lambda i: i.default_action if name == "DEFAULT" else Action[name])

    def action_ignore(self) -> None:
        """Leave the marked rows (or the current one) out of this round, or take
        them back; a folder takes its content along.  Comparing again resets this."""
        if self.cmp is None:
            return
        targets = self._targets()
        if not targets:
            return
        on = not all(i.ignored for i in targets)
        cursor = self.query_one(PlanTable).cursor_row
        changed, failed = self.cmp.set_ignored(targets, on)
        self.marked -= {i.path for i in targets}
        self._refresh_table()
        if self.rows:
            self.query_one(PlanTable).move_cursor(row=min(cursor + (not on), len(self.rows) - 1))
        if failed:
            self.notify(f"{failed} item(s) belong to a renamed folder: ignore the folder instead",
                        severity="warning")
        elif changed > 1 or len(targets) > 1:
            self.notify(f"{changed} item(s) {'ignored this time' if on else 'taken back'}")

    def action_cycle(self) -> None:
        item = self._current()
        if item is None:
            return
        options = item.allowed_actions()
        if item.default_action not in options:
            options.insert(0, item.default_action)
        nxt = options[(options.index(item.action) + 1) % len(options)] if item.action in options else options[0]
        self._apply(item, nxt)

    @on(PlanTable.ActionClicked)
    def _action_clicked(self, event: PlanTable.ActionClicked) -> None:
        self.query_one(PlanTable).move_cursor(row=event.row)
        self.action_cycle()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        # Enter, or a click on the already highlighted row, opens the details
        if time.monotonic() - self.query_one(PlanTable).action_click_time > 0.5:
            self.action_details()

    def action_details(self) -> None:
        item = self._current()
        if item is None:
            return

        def done(action: Action | None) -> None:
            if action is not None:
                self._apply(item, action)
        self.app.push_screen(ItemScreen(item), done)

    def action_resolve_newer(self) -> None:
        if self.cmp is None:
            return
        n = 0
        for item in self.cmp.items:
            if item.conflict and item.action == Action.SKIP and not item.locked and not item.ignored:
                if item.left and item.right:
                    act = Action.COPY_LR if item.left.mtime >= item.right.mtime else Action.COPY_RL
                else:
                    act = Action.COPY_LR if item.left else Action.COPY_RL
                self.cmp.set_action(item, act)
                n += 1
        self._refresh_table()
        self.notify(f"{n} conflict(s) resolved: newer side wins")

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "compare":
            self.action_compare()
        elif bid == "sync":
            self.action_sync()
        elif bid == "back":
            self.action_back()
        elif bid == "select":
            self.action_select()
        elif bid.startswith("act-"):
            self.action_set_action(bid[4:])
        elif bid.startswith("dir-"):
            self.action_direction(bid[4:])

    # -- syncing -------------------------------------------------------------
    def action_sync(self) -> None:
        if self.cmp is None or self.workers_running():
            return
        pending = self.cmp.pending()
        if not pending:
            self.notify("Nothing to synchronize")
            return
        c = self.cmp.counts()
        rows = [
            ("Copy to the right", c[Action.COPY_LR.value]),
            ("Copy to the left", c[Action.COPY_RL.value]),
            ("Renames (no transfer)", c[Action.MOVE_R.value] + c[Action.MOVE_L.value]),
            ("Time/permission updates", c[Action.ATTR_LR.value] + c[Action.ATTR_RL.value]),
            *([("Name spelling fixes", c["names"])] if c["names"] else []),
            ("Delete on the left", c[Action.DELETE_L.value]),
            ("Delete on the right", c[Action.DELETE_R.value]),
            *([(f"Free on the {self.job.selective} (verified)",
                f"{c[Action.EVICT_L.value] + c[Action.EVICT_R.value]} + uploaded ones, "
                f"{fmt.size(self.cmp.freed_bytes())}")] if self.job.selective else []),
            ("Unresolved conflicts (skipped)", c["conflict"]),
            *([("Ignored this time", c["ignored"])] if c["ignored"] else []),
            ("Data to transfer", fmt.size(self.cmp.transfer_bytes())),
            ("Deleted/overwritten files",
             "moved to trash" if self.job.deletion == "trash" else "deleted permanently"),
        ]
        body = "\n".join(f"{label + ':':<32}{value}" for label, value in rows)
        problem = self.cmp.safety_problem()
        if problem:
            body += f"\n\n[bold red]⚠ {problem}.[/]\nCheck the plan carefully before continuing."

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.app.push_screen(SyncScreen(self.cmp, force=bool(problem)), self._sync_closed)
        self.app.push_screen(ConfirmScreen("Start synchronization?", body,
                                           "Synchronize anyway" if problem else "Synchronize",
                                           danger=bool(problem)), confirmed)

    def _sync_closed(self, result: SyncResult | None) -> None:
        self._refresh_table()
        if result is not None and not result.failed and not result.cancelled:
            self.action_compare()

    def action_edit(self) -> None:
        if self.job.file is None:
            return
        with self.app.suspend():
            subprocess.call(editor_command(self.job.file))
        try:
            self.job = config_mod.load_job_file(self.job.file, config_mod.load_global_defaults())
        except config_mod.ConfigError as e:
            self.notify(str(e), severity="error", timeout=10)
            return
        self.query_one("#job-info", Static).update(self._job_info())
        self.action_compare()

    def action_select(self) -> None:
        if not self.job.selective:
            self.notify('Selective sync is off for this job: add  selective = "left"  (the side that '
                        "keeps only a selection) to the job file.", severity="warning", timeout=10)
            return
        from mysync.tui.select import SelectScreen

        def closed(saved: bool | None) -> None:
            if saved:
                self.action_compare()
        self.app.push_screen(SelectScreen(self.job), closed)

    def action_back(self) -> None:
        self.app.pop_screen()


# --------------------------------------------------------------------------
# job list


class JobsScreen(Screen):
    BINDINGS = [
        Binding("n", "new", "New job"),
        Binding("e", "edit", "Edit"),
        Binding("r", "reload", "Reload"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.jobs: list[config_mod.JobConfig] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Jobs in [b]{paths.conf_dir()}[/b]   (click a job to open it)", id="jobs-info")
        yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
        yield Static("", id="jobs-errors")
        with Horizontal(classes="buttons"):
            yield Button("Open", id="open", variant="primary")
            yield Button("New job", id="new", variant="success")
            yield Button("Edit", id="edit")
            yield Button("Reload", id="reload")
            yield Button("Quit", id="quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "mysync"
        table = self.query_one("#jobs", DataTable)
        table.add_columns("Job", "Mode", "Left", "Right", "Last sync")
        self.action_reload()

    def on_screen_resume(self) -> None:
        self.action_reload()

    def action_reload(self) -> None:
        result = config_mod.load_all()
        self.jobs = result.jobs
        table = self.query_one("#jobs", DataTable)
        row = table.cursor_row
        table.clear()
        for job in self.jobs:
            last = state_mod.last_sync_time(job)
            table.add_row(Text(job.name, style="bold"), job.mode, job.left.url, job.right.url,
                          fmt.mtime(last) if last else "never", key=job.name)
        if self.jobs:
            table.move_cursor(row=min(max(row, 0), len(self.jobs) - 1))
        errors = "\n".join(f"[red]{e}[/]" for e in result.errors)
        if not self.jobs and not errors:
            errors = "No jobs yet – press [b]New job[/b] to create one."
        self.query_one("#jobs-errors", Static).update(errors)

    def _current(self) -> config_mod.JobConfig | None:
        table = self.query_one("#jobs", DataTable)
        if 0 <= table.cursor_row < len(self.jobs):
            return self.jobs[table.cursor_row]
        return None

    @on(DataTable.RowSelected, "#jobs")
    def _selected(self) -> None:
        self.action_open()

    def action_open(self) -> None:
        job = self._current()
        if job:
            self.app.push_screen(JobScreen(job))

    def action_new(self) -> None:
        def created(result: str | None) -> None:
            if not result:
                return
            if result.startswith("edit:"):
                self._edit_file(result[5:])
            self.action_reload()
        self.app.push_screen(NewJobScreen(), created)

    def _edit_file(self, path) -> None:
        with self.app.suspend():
            subprocess.call(editor_command(path))

    def action_edit(self) -> None:
        job = self._current()
        if job and job.file:
            self._edit_file(job.file)
            self.action_reload()

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        actions = {"open": self.action_open, "new": self.action_new, "edit": self.action_edit,
                   "reload": self.action_reload, "quit": self.app.exit}
        if event.button.id in actions:
            actions[event.button.id]()


class MysyncApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "mysync"
    ENABLE_COMMAND_PALETTE = False

    def __init__(self, initial_job: str | None = None, select: bool = False):
        super().__init__()
        self.initial_job = initial_job
        self.select = select

    def on_mount(self) -> None:
        self.push_screen(JobsScreen())
        if self.initial_job:
            try:
                job = config_mod.find_job(self.initial_job)
                if self.select:
                    from mysync.tui.select import SelectScreen

                    def closed(saved: bool | None) -> None:
                        if saved:  # review the resulting plan
                            self.push_screen(JobScreen(job))
                        else:
                            self.exit()
                    self.push_screen(SelectScreen(job), closed)
                else:
                    self.push_screen(JobScreen(job))
            except config_mod.ConfigError as e:
                self.notify(str(e), severity="error", timeout=10)
