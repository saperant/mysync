"""Selective sync: which parts of the tree are kept on the "partial" side.

A job with ``selective = "left"`` (or ``"right"``) keeps only the selected
folders and files on that side; the other side holds everything.  The
selection is a set of path rules, stored per job in
``~/.config/mysync/selection/<job>.toml``::

    default = true                 # what is not covered by a rule
    selected = ["Projects/2026"]
    deselected = ["Projects"]

The most specific rule (the path itself or its nearest ancestor with a rule)
decides.  Unlike filters, a selection is applied asymmetrically:

* outside the selection nothing is downloaded and nothing is ever deleted on
  the full side;
* what is still present locally outside the selection is removed there
  ("freed") once the full side is verified to hold the same content; local
  changes are uploaded first.
"""

from __future__ import annotations

import json
import os
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from mysync import paths
from mysync.config import ConfigError, JobConfig

ALL, NONE, SOME = "all", "none", "some"


def _parents(path: str) -> list[str]:
    """The path itself and its ancestors, innermost first ('' is the root)."""
    out = [path]
    while path:
        path = path.rsplit("/", 1)[0] if "/" in path else ""
        out.append(path)
    return out


def norm(path: str) -> str:
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"invalid path {path!r}")
    return "/".join(parts)


@dataclass
class Selection:
    rules: dict[str, bool] = field(default_factory=dict)  # path -> selected; "" is the root

    @property
    def default(self) -> bool:
        return self.rules.get("", True)

    def is_selected(self, path: str) -> bool:
        for p in _parents(path):
            if p in self.rules:
                return self.rules[p]
        return True

    def _below(self, path: str) -> list[tuple[str, bool]]:
        prefix = path + "/" if path else ""
        return [(p, v) for p, v in self.rules.items() if p and p.startswith(prefix) and p != path]

    def state(self, path: str) -> str:
        """ALL, NONE or SOME (a folder with differently selected content)."""
        own = self.is_selected(path)
        if any(v != own for _, v in self._below(path)):
            return SOME
        return ALL if own else NONE

    def wanted(self, path: str, is_dir: bool) -> bool:
        """Does the selection reach this path (needs to be scanned)?"""
        if self.is_selected(path):
            return True
        return is_dir and any(v for _, v in self._below(path))

    def set(self, path: str, selected: bool) -> None:
        for p, _ in self._below(path):
            del self.rules[p]
        self.rules.pop(path, None)
        if path == "" or self.is_selected(path) != selected:
            self.rules[path] = selected
        if self.rules.get("") is True:
            del self.rules[""]

    def toggle(self, path: str) -> bool:
        """Select a not fully selected item, otherwise deselect it."""
        new = self.state(path) != ALL
        self.set(path, new)
        return new

    @property
    def is_everything(self) -> bool:
        return not self.rules or all(self.rules.values())


def selection_file(job: JobConfig) -> Path:
    return paths.config_home() / "selection" / f"{job.name}.toml"


def load(job: JobConfig) -> Selection:
    path = selection_file(job)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return Selection()
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    sel = Selection()
    if not data.get("default", True):
        sel.rules[""] = False
    for key, value in (("deselected", False), ("selected", True)):
        items = data.get(key, [])
        if not isinstance(items, list) or not all(isinstance(p, str) for p in items):
            raise ConfigError(f"{path}: '{key}' must be a list of paths")
        for p in items:
            sel.rules[norm(p)] = value
    return sel


def save(job: JobConfig, sel: Selection) -> Path:
    path = selection_file(job)
    path.parent.mkdir(parents=True, exist_ok=True)

    def plist(value: bool) -> str:
        items = sorted(p for p, v in sel.rules.items() if p and v == value)
        if not items:
            return "[]"
        return "[\n" + "".join(f"    {json.dumps(p, ensure_ascii=False)},\n" for p in items) + "]"
    text = (f"# Selective sync of job {json.dumps(job.name, ensure_ascii=False)}: what is kept on the "
            f"{job.selective} side.\n"
            "# Written by `mysync select`; paths are relative to the location root.\n"
            f"default = {'true' if sel.default else 'false'}\n"
            f"selected = {plist(True)}\n"
            f"deselected = {plist(False)}\n")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
