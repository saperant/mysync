"""mysync's user interface on the myrcp workbench (https://github.com/saperant/myrcp):
this directory is a myrcp product (product.toml, plugins/mysync), started by `mysync`
or `mysync ui`. myrcp comes from PyPI with mysync; it brings the terminal workbench,
and runs the GTK one (myrcp-gtk) when that is installed."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

PRODUCT = Path(__file__).resolve().parent


def run(args: Sequence[str] = ()) -> None:
    """Replace this process with the workbench; ``args`` are the workbench's options."""
    from myrcp.launch import run_product

    run_product(PRODUCT, list(args))
