"""Unattended crash recording.

``run.bat`` ties the console to the app, so the moment the process dies the
window (and any traceback printed to it) is gone. These hooks write every
unhandled error -- Python exceptions on any thread, and native faults via
faulthandler -- to ``crash.log`` next to the settings file, so a crash
leaves something behind to actually read.
"""

from __future__ import annotations

import faulthandler
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from .config import project_root

#: Where unattended errors land. Truncated at every launch: the file always
#: describes the most recent run, which is the one that crashed.
LOG_PATH = project_root() / "crash.log"

_handle = None


def _write(exc_type, exc, tb) -> None:
    """Append one report to the log; never raise (we are already dying)."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== unhandled error {datetime.now().isoformat()} ===\n")
            traceback.print_exception(exc_type, exc, tb, file=fh)
            fh.flush()
    except OSError:
        pass


def _sys_hook(exc_type, exc, tb) -> None:
    """sys.excepthook: log, echo to the console, show the user if we can."""
    _write(exc_type, exc, tb)
    try:
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)
    except OSError:
        pass
    # On the GUI thread a visible error beats a silent death -- but only if
    # a Qt application actually exists, and never from a worker thread.
    if (
        threading.current_thread() is threading.main_thread()
        and exc_type is not SystemExit
    ):
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox

            app = QApplication.instance()
            if app is not None:
                QMessageBox.critical(
                    None,
                    "DroidMirror crashed",
                    f"{exc_type.__name__}: {exc}\n\n"
                    f"The full traceback was saved to:\n{LOG_PATH}",
                )
        except Exception:
            pass  # the box is a courtesy; the log is the contract
    sys.__excepthook__(exc_type, exc, tb)


def _thread_hook(args) -> None:
    """threading.excepthook: a worker thread dying must still leave a trace."""
    _write(args.exc_type, args.exc_value, args.exc_traceback)
    try:
        traceback.print_exception(
            args.exc_type, args.exc_value, args.exc_traceback, file=sys.stderr
        )
    except OSError:
        pass


def install() -> Path:
    """Arm the hooks (fresh log) and return the log path."""
    global _handle
    if _handle is not None:
        try:
            faulthandler.disable()
        except Exception:
            pass
        try:
            _handle.close()
        except OSError:
            pass
        _handle = None
    try:
        open(LOG_PATH, "w", encoding="utf-8").close()
    except OSError:
        pass
    # Native faults (access violations, aborts) bypass sys.excepthook; the
    # handler writes straight from the crashing process state.
    _handle = open(LOG_PATH, "a", encoding="utf-8")
    faulthandler.enable(file=_handle, all_threads=True)
    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    return LOG_PATH
