"""mysync's user interface on the myrcp workbench (https://github.com/saperant/myrcp):
this directory is a myrcp product (product.toml, plugins/mysync), started by the
`mysync-ui` command. Needs the `ui` extra: pip install 'mysync[ui]'.

The Textual TUI (`mysync`, mysync.tui) needs no extra; this is the same engine in the
myrcp workbench, next to the user's editor, terminals and other tools."""

from __future__ import annotations

import sys
from pathlib import Path

PRODUCT = Path(__file__).resolve().parent


def main() -> None:
    try:
        from myrcp.launch import run_product
    except ImportError:
        print("mysync-ui needs myrcp: pip install 'mysync[ui]'", file=sys.stderr)
        raise SystemExit(1) from None
    run_product(PRODUCT)
