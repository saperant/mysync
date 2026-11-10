"""Selective sync: a tree browser for choosing what is kept on the selective side."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.style import Style
from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

from mysync import config as config_mod
from mysync import fmt
from mysync import selection as selection_mod
from mysync.backends import open_backend
from mysync.compare import builtin_excludes
from mysync.filters import PathFilter
from mysync.model import DIR, FILE, Entry, SyncError
from mysync.selection import ALL, NONE, Selection

_CHECK = {ALL: ("[x]", "bold green"), NONE: ("[ ]", "dim"), selection_mod.SOME: ("[-]", "bold yellow")}
_CHECK_META = Style.from_meta({"check": True})


@dataclass
class _Node:
    path: str
    is_dir: bool
    full_size: int = 0  # on the side that holds everything
    local_size: int = 0  # on the selective side
    full: bool = False  # exists on the full side
    local: bool = False  # exists on the selective side
    children: list[str] = field(default_factory=list)


def build_index(full: dict[str, Entry], local: dict[str, Entry]) -> dict[str, _Node]:
    """Folder tree of both sides with total sizes per folder."""
    nodes: dict[str, _Node] = {"": _Node("", True, full=True, local=True)}

    def node_for(path: str, is_dir: bool) -> _Node:
        node = nodes.get(path)
        if node is None:
            parent = path.rsplit("/", 1)[0] if "/" in path else ""
            node_for(parent, True).children.append(path)
            node = nodes[path] = _Node(path, is_dir)
        return node

    for scan, is_full in ((full, True), (local, False)):
        for path in sorted(scan):
            e = scan[path]
            node = node_for(path, e.kind == DIR)
            if is_full:
                node.full = True
            else:
                node.local = True
            if e.kind != FILE:
                continue
            p = path
            while True:
                if is_full:
                    nodes[p].full_size += e.size
                else:
                    nodes[p].local_size += e.size
                if not p:
                    break
                p = p.rsplit("/", 1)[0] if "/" in p else ""
    for node in nodes.values():
        node.children.sort(key=lambda c: (not nodes[c].is_dir, c.lower()))
    return nodes


def plan_totals(nodes: dict[str, _Node], full: dict[str, Entry], local: dict[str, Entry],
                sel: Selection) -> tuple[int, int, int, int]:
    """(selected size, total size, to download, to free) for the summary."""
    selected = total = download = free = 0
    for path, e in full.items():
        if e.kind != FILE:
            continue
        total += e.size
        if sel.is_selected(path):
            selected += e.size
            if path not in local:
                download += e.size
    for path, e in local.items():
        if e.kind == FILE and not sel.is_selected(path):
            free += e.size
    return selected, total, download, free


class SelectTree(Tree[_Node]):
    """Tree with a selection box in front of every item.

    Click the box (or press space) to toggle; click the arrow or the name of a
    folder (or press enter / → / ←) to open and close it.
    """

    BINDINGS = [
        Binding("space", "toggle_selected", "Toggle selection"),
        Binding("enter", "toggle_node", "Open/close", show=False),
        Binding("right", "open", "Open", show=False),
        Binding("left", "close", "Close", show=False),
    ]

    class Toggled(Message):
        def __init__(self, node: TreeNode[_Node]):
            super().__init__()
            self.node = node

    def __init__(self, sel: Selection, **kwargs):
        super().__init__("/", **kwargs)
        self.sel = sel
        self.auto_expand = False
        self.guide_depth = 3

    def render_label(self, node: TreeNode[_Node], base_style: Style, style: Style) -> Text:
        text = super().render_label(node, base_style, style)
        data = node.data
        if data is None:
            return text
        box, box_style = _CHECK[self.sel.state(data.path)]
        prefix = Text.assemble((box, Style.parse(box_style) + _CHECK_META), " ")
        # keep the arrow (it carries the open/close click target) in front
        arrow_len = len(self.ICON_NODE) if node.allow_expand else 0
        out = text[:arrow_len] + prefix + text[arrow_len:]
        size = data.full_size if data.full else data.local_size
        info = []
        if data.is_dir or size:
            info.append(fmt.size(size))
        if not data.full:
            info.append("only here: uploaded at next sync")
        elif data.local and data.is_dir and data.local_size and data.local_size != data.full_size:
            info.append(f"{fmt.size(data.local_size)} local")
        elif data.local:
            info.append("local")
        out.append("  " + " · ".join(info), style="dim")
        return out

    async def _on_click(self, event: events.Click) -> None:
        meta = event.style.meta
        if "line" in meta and meta.get("check"):
            event.stop()
            self.cursor_line = meta["line"]
            node = self.get_node_at_line(meta["line"])
            if node is not None:
                self.post_message(self.Toggled(node))
            return
        if "line" in meta and not meta.get("toggle"):
            event.stop()
            self.cursor_line = meta["line"]
            node = self.get_node_at_line(meta["line"])
            if node is not None and node.allow_expand:
                node.toggle()
            return
        await super()._on_click(event)

    def action_toggle_selected(self) -> None:
        if self.cursor_node is not None:
            self.post_message(self.Toggled(self.cursor_node))

    def action_open(self) -> None:
        if self.cursor_node is not None and self.cursor_node.allow_expand:
            self.cursor_node.expand()

    def action_close(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.is_expanded:
            node.collapse()
        elif node.parent is not None:
            self.cursor_line = node.parent.line

    def redraw(self) -> None:
        self._invalidate()


class SelectScreen(Screen[bool]):
    """Choose the folders and files the selective side keeps."""

    BINDINGS = [
        Binding("ctrl+s,w", "save", "Save"),
        Binding("a", "everything", "Select all"),
        Binding("escape,q", "back", "Back"),
    ]

    def __init__(self, job: config_mod.JobConfig):
        super().__init__()
        self.job = job
        self.partial = job.selective
        self.full_side = "right" if job.selective == "left" else "left"
        self.sel = selection_mod.load(job)
        self.saved_rules = dict(self.sel.rules)
        self.nodes: dict[str, _Node] = {}
        self.full: dict[str, Entry] = {}
        self.local: dict[str, Entry] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        loc = getattr(self.job, self.partial)
        other = getattr(self.job, self.full_side)
        yield Static(f"[b]{self.job.name}[/b] – selective sync: choose what [b]{loc.url}[/b] keeps.\n"
                     f"[dim]Everything stays on {other.url}. Unselected items are not downloaded; "
                     "local copies of them are uploaded if needed, verified and then removed.[/dim]",
                     id="job-info")
        yield SelectTree(self.sel, id="select-tree")
        yield Static("Scanning…", id="summary")
        with Horizontal(classes="buttons"):
            yield Button("Save", id="save", variant="primary", disabled=True)
            yield Button("Select all", id="everything")
            yield Button("Expand all", id="expand")
            yield Button("Back", id="back")
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"mysync – {self.job.name} – selection"
        self._scan()

    @work(thread=True, exclusive=True)
    def _scan(self) -> None:
        job = self.job
        flt = PathFilter(job.include, job.exclude + builtin_excludes(job))
        try:
            with open_backend(job.left) as lb, open_backend(job.right) as rb:
                scans = {}
                for side, backend in (("left", lb), ("right", rb)):
                    self.app.call_from_thread(self._status, f"Scanning the {side}…")
                    scans[side] = backend.scan(flt.excluded, job.symlinks) if backend.root_exists() else {}
        except (SyncError, OSError) as e:
            self.app.call_from_thread(self._status, f"[red]Scanning failed:[/red] {e}")
            return
        self.app.call_from_thread(self._loaded, scans[self.full_side], scans[self.partial])

    def _status(self, text: str) -> None:
        self.query_one("#summary", Static).update(text)

    def _loaded(self, full: dict[str, Entry], local: dict[str, Entry]) -> None:
        self.full, self.local = full, local
        self.nodes = build_index(full, local)
        tree = self.query_one(SelectTree)
        tree.root.data = self.nodes[""]
        self._add_children(tree.root)
        tree.root.expand()
        tree.focus()
        self._update_summary()

    def _add_children(self, node: TreeNode[_Node]) -> None:
        if node.data is None or node.children:
            return
        for child in node.data.children:
            data = self.nodes[child]
            name = child.rsplit("/", 1)[-1] + ("/" if data.is_dir else "")
            label = Text(name, style="bold" if data.is_dir else "")
            if data.is_dir and data.children:
                node.add(label, data)
            else:
                node.add_leaf(label, data)

    @on(Tree.NodeExpanded)
    def _expanded(self, event: Tree.NodeExpanded) -> None:
        self._add_children(event.node)

    @on(SelectTree.Toggled)
    def _toggled(self, event: SelectTree.Toggled) -> None:
        data = event.node.data
        if data is None:
            return
        self.sel.toggle(data.path)
        self.query_one(SelectTree).redraw()
        self._update_summary()

    def _changed(self) -> bool:
        return self.sel.rules != self.saved_rules

    def _update_summary(self) -> None:
        if not self.nodes:
            return
        selected, total, download, free = plan_totals(self.nodes, self.full, self.local, self.sel)
        text = (f"Selected [b]{fmt.size(selected)}[/b] of {fmt.size(total)} on the {self.full_side}.   "
                f"Next sync: [cyan]download {fmt.size(download)}[/cyan], "
                f"[yellow]free {fmt.size(free)}[/yellow] on the {self.partial}.")
        if self._changed():
            text += "   [bold]Not saved.[/bold]"
        self._status(text)
        self.query_one("#save", Button).disabled = not self._changed()

    def action_save(self) -> None:
        if not self._changed():
            self.dismiss(False)
            return
        path = selection_mod.save(self.job, self.sel)
        self.saved_rules = dict(self.sel.rules)
        self.notify(f"Saved {path}. Compare and synchronize to apply it.")
        self.dismiss(True)

    def action_everything(self) -> None:
        self.sel.set("", True)
        self.query_one(SelectTree).redraw()
        self._update_summary()

    def action_back(self) -> None:
        if not self._changed():
            self.dismiss(False)
            return
        from mysync.tui.app import ConfirmScreen

        def answered(discard: bool | None) -> None:
            if discard:
                self.dismiss(False)
        self.app.push_screen(ConfirmScreen("Discard changes?", "The selection has not been saved.",
                                           "Discard", danger=True), answered)

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "save":
            self.action_save()
        elif bid == "everything":
            self.action_everything()
        elif bid == "expand":
            tree = self.query_one(SelectTree)
            stack = [tree.root]
            while stack:  # load every level, then open them all
                node = stack.pop()
                self._add_children(node)
                stack.extend(node.children)
            tree.root.expand_all()
        elif bid == "back":
            self.action_back()
