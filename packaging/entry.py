"""Frozen-build entry point.

``python -m droidmirror`` cannot be handed to PyInstaller directly -- it freezes
a *module*, not a package. This file is the smallest possible shim: make the
windowed executable behave like ``pythonw -m droidmirror`` and hand straight
over to the real CLI, so ``--check``, ``--list-devices`` and plain GUI launches
all keep working in the built .exe.

Kept deliberately import-light: nothing heavy is touched until ``main()`` runs,
which keeps a broken install from failing before the crash log is installed.
"""

from __future__ import annotations

import io
import os
import sys


def _quiet_streams() -> None:
    """Give a windowed .exe working stdio.

    A ``console=False`` PyInstaller build has ``sys.stdout``/``sys.stderr`` set
    to ``None``. ``print()`` then raises ``AttributeError`` -- which would turn
    ``--check`` (and any diagnostic print on a failure path) into a crash. Point
    them at the bit bucket instead; a console build is unaffected.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = io.StringIO()


def main() -> int:
    _quiet_streams()
    from droidmirror.__main__ import main as cli_main

    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
