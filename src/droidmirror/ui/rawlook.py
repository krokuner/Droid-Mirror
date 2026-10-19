"""Relative mouse movement for button-free mouse-look (Windows).

Steering a camera from *cursor position* has an unavoidable flaw: the cursor
can only travel to the edge of the mirror, and every re-centre the app does
is indistinguishable from the user moving the other way. Reading successive
cursor positions and shifting the pointer back can therefore cancel its own
deltas out, which shows up as "the cursor locks and the camera stops".

Windows can hand out mouse motion *before* it becomes a cursor position:
register the window for raw input and the system posts WM_INPUT carrying the
device's own relative deltas. Those deltas are unaffected by where the cursor
is, by screen edges, by cursor clipping and by the pointer-acceleration
curve, so the look can spin forever without ever pinning the pointer.

The deltas pile up between polls; the owner drains them with ``take()`` at
whatever rate it injects at. ``nativeEventFilter`` and ``take`` both run on
the GUI thread, so no locking is needed.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

#: WM_INPUT: a raw input event was posted to the registered window.
_WM_INPUT = 0x00FF
#: GetRawInputData: give me the RAWINPUT for this handle.
_RID_INPUT = 0x10000003
#: Register even when the window is not in the foreground.
_RIDEV_INPUTSINK = 0x00000100
#: Undo a previous RegisterRawInputDevices call.
_RIDEV_REMOVE = 0x00000001
#: Generic desktop controls / mouse (usUsagePage, usUsage).
_HID_USAGE_PAGE_GENERIC = 0x01
_HID_USAGE_GENERIC_MOUSE = 0x02
_RIM_TYPE_MOUSE = 0


if sys.platform == "win32":
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    class _RawButtons(ctypes.Structure):
        _fields_ = [
            ("usButtonFlags", wintypes.USHORT),
            ("usButtonData", wintypes.USHORT),
        ]

    class _RawButtonsUnion(ctypes.Union):
        _fields_ = [
            ("ulButtons", wintypes.ULONG),
            ("buttons", _RawButtons),
        ]

    class RAWMOUSE(ctypes.Structure):
        _fields_ = [
            ("usFlags", wintypes.USHORT),
            ("u", _RawButtonsUnion),
            ("ulRawButtons", wintypes.ULONG),
            ("lLastX", wintypes.LONG),
            ("lLastY", wintypes.LONG),
            ("ulExtraInformation", wintypes.ULONG),
        ]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [
            ("dwType", wintypes.DWORD),
            ("dwSize", wintypes.DWORD),
            ("hDevice", wintypes.HANDLE),
            ("wParam", wintypes.WPARAM),
        ]

    class RAWINPUT(ctypes.Structure):
        _fields_ = [("header", RAWINPUTHEADER), ("mouse", RAWMOUSE)]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [
            ("usUsagePage", wintypes.USHORT),
            ("usUsage", wintypes.USHORT),
            ("dwFlags", wintypes.DWORD),
            ("hwndTarget", wintypes.HWND),
        ]

    _user32.RegisterRawInputDevices.argtypes = [
        ctypes.POINTER(RAWINPUTDEVICE),
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.RegisterRawInputDevices.restype = wintypes.BOOL
    _user32.GetRawInputData.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.UINT),
        wintypes.UINT,
    ]
    _user32.GetRawInputData.restype = wintypes.UINT


def _message_address(message) -> int:
    """PySide6 hands the filter an opaque pointer; get its integer value."""
    try:
        return int(message)
    except (TypeError, ValueError):
        try:
            return int(message.__int__())
        except Exception:
            return 0


class RawMouseLook(QAbstractNativeEventFilter):
    """Accumulates raw mouse deltas while a window is registered.

    ``start(hwnd)`` returns False (and changes nothing) when raw input is not
    available, so the caller can fall back to cursor polling.
    """

    def __init__(self) -> None:
        super().__init__()
        self._dx = 0
        self._dy = 0
        self._hwnd = 0
        self._installed = False

    # -- capability ------------------------------------------------------- #

    @staticmethod
    def supported() -> bool:
        return sys.platform == "win32"

    @property
    def active(self) -> bool:
        """True while a window is registered and deltas are being collected."""
        return bool(self._hwnd)

    # -- lifecycle -------------------------------------------------------- #

    def start(self, hwnd: int) -> bool:
        """Begin collecting deltas posted to ``hwnd``. Idempotent."""
        if self.active:
            return True
        if not self.supported() or not hwnd:
            return False
        app = QCoreApplication.instance()
        if app is None:
            return False
        device = RAWINPUTDEVICE(
            _HID_USAGE_PAGE_GENERIC,
            _HID_USAGE_GENERIC_MOUSE,
            _RIDEV_INPUTSINK,
            hwnd,
        )
        if not _user32.RegisterRawInputDevices(
            ctypes.byref(device), 1, ctypes.sizeof(RAWINPUTDEVICE)
        ):
            return False
        # The filter must outlive this call: keep it referenced by the caller
        # (MainWindow holds us) and only ever install once.
        if not self._installed:
            app.installNativeEventFilter(self)
            self._installed = True
        self._hwnd = int(hwnd)
        self._dx = 0
        self._dy = 0
        return True

    def stop(self) -> None:
        """Stop collecting; the filter itself stays installed but inert."""
        if not self.active:
            return
        self._hwnd = 0
        self._dx = 0
        self._dy = 0
        device = RAWINPUTDEVICE(
            _HID_USAGE_PAGE_GENERIC,
            _HID_USAGE_GENERIC_MOUSE,
            _RIDEV_REMOVE,
            None,
        )
        try:
            _user32.RegisterRawInputDevices(
                ctypes.byref(device), 1, ctypes.sizeof(RAWINPUTDEVICE)
            )
        except Exception:
            pass  # best-effort: worst case we keep receiving WM_INPUT

    # -- deltas ----------------------------------------------------------- #

    def take(self) -> tuple[int, int]:
        """Return and clear the movement accumulated since the last call."""
        dx, dy = self._dx, self._dy
        self._dx = self._dy = 0
        return dx, dy

    def nativeEventFilter(self, eventType, message):  # noqa: N802
        if not self.active:
            return False, 0
        if eventType not in (b"windows_generic_MSG", "windows_generic_MSG"):
            return False, 0
        address = _message_address(message)
        if not address:
            return False, 0
        try:
            msg = wintypes.MSG.from_address(address)
        except Exception:
            return False, 0
        if msg.message == _WM_INPUT:
            self._collect(msg.lParam)
        return False, 0

    def _collect(self, raw_handle) -> None:
        """Read one RAWINPUT and accumulate its mouse movement."""
        size = wintypes.UINT(ctypes.sizeof(RAWINPUT))
        buffer = ctypes.create_string_buffer(ctypes.sizeof(RAWINPUT))
        got = _user32.GetRawInputData(
            raw_handle,
            _RID_INPUT,
            buffer,
            ctypes.byref(size),
            ctypes.sizeof(RAWINPUTHEADER),
        )
        # -1 (UINT_MAX) on failure; anything shorter than the header is junk.
        if got in (0, 0xFFFFFFFF) or got < ctypes.sizeof(RAWINPUTHEADER):
            return
        raw = RAWINPUT.from_buffer(buffer)
        if raw.header.dwType != _RIM_TYPE_MOUSE:
            return
        self._dx += raw.mouse.lLastX
        self._dy += raw.mouse.lLastY
