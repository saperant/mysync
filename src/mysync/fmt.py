"""Formatting helpers shared by the CLI and the TUI."""

from __future__ import annotations

import time

from mysync.model import Action, Category, Entry

ACTION_SYMBOL = {
    Action.NONE: "=",
    Action.COPY_LR: "->",
    Action.COPY_RL: "<-",
    Action.MOVE_R: "mv>",
    Action.MOVE_L: "<mv",
    Action.ATTR_LR: "at>",
    Action.ATTR_RL: "<at",
    Action.DELETE_L: "x-",
    Action.DELETE_R: "-x",
    Action.EVICT_L: "<fr",
    Action.EVICT_R: "fr>",
    Action.SKIP: "--",
}

CATEGORY_LABEL = {
    Category.EQUAL: "equal",
    Category.LEFT_ONLY: "left only",
    Category.RIGHT_ONLY: "right only",
    Category.LEFT_NEWER: "left newer",
    Category.RIGHT_NEWER: "right newer",
    Category.DIFFERENT: "different",
    Category.ATTRIBUTES: "attributes differ",
    Category.CONFLICT: "conflict",
}


def size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def mtime(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def entry(e: Entry | None) -> str:
    if e is None:
        return ""
    if e.kind == "d":
        return "<folder>"
    return f"{size(e.size):>10}  {mtime(e.mtime)}"


def duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
