"""Include/exclude path filtering with gitignore-like glob patterns.

Pattern rules:
  * ``*`` matches anything except ``/``, ``?`` one character, ``[...]`` a set.
  * ``**`` matches across directories (``**/build``, ``docs/**``).
  * A pattern without ``/`` matches the item name at any depth (``*.tmp``).
  * A pattern containing ``/`` is anchored at the sync root (``/cache``, ``src/*.o``).
  * A trailing ``/`` restricts the pattern to directories (``node_modules/``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


def _glob_to_regex(glob: str) -> str:
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if glob.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if glob.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = glob.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                body = glob[i + 1 : j]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append("[" + body.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


@dataclass(frozen=True)
class _Pattern:
    regex: re.Pattern[str]
    dir_only: bool

    @classmethod
    def parse(cls, pattern: str) -> "_Pattern":
        dir_only = pattern.endswith("/")
        body = pattern.rstrip("/")
        anchored = "/" in body
        body = body.lstrip("/")
        prefix = "" if anchored else "(?:.*/)?"
        return cls(re.compile(f"^{prefix}{_glob_to_regex(body)}$"), dir_only)

    def matches(self, rel: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        return self.regex.match(rel) is not None


class PathFilter:
    def __init__(self, include: list[str] | None = None, exclude: list[str] | None = None):
        self._include = [_Pattern.parse(p) for p in include or [] if p.strip()]
        self._exclude = [_Pattern.parse(p) for p in exclude or [] if p.strip()]

    def excluded(self, rel: str, is_dir: bool) -> bool:
        """True if ``rel`` (relative POSIX path) must be ignored.

        Excluding a directory excludes everything below it.  Include patterns
        only restrict files; directories are always traversed.
        """
        if any(p.matches(rel, is_dir) for p in self._exclude):
            return True
        if self._include and not is_dir:
            return not any(p.matches(rel, is_dir) for p in self._include)
        return False
