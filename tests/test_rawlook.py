"""Raw mouse input: struct layout and WM_INPUT parsing.

Raw input is what makes mouse-look work without pinning the cursor, and it
depends on a hand-rolled ctypes view of RAWINPUT -- a silent layout mistake
would move the camera by whatever bytes happened to sit where ``lLastX``
should be. These tests pin the layout to the documented Win32 one, then feed
a synthetic WM_INPUT through the real filter method.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from droidmirror.ui import rawlook  # noqa: E402
from droidmirror.ui.rawlook import RawMouseLook  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="raw input is a Windows API"
)

WM_INPUT = 0x00FF
RAW_HANDLE = 0xDEAD


class FakeUser32:
    """Stands in for user32's raw-input helper."""

    def __init__(self, raw: bytes, result: int | None = None) -> None:
        self.raw = raw
        self.result = len(raw) if result is None else result
        self.asked_for = None

    def GetRawInputData(self, handle, kind, buffer, size_ref, header_size):
        self.asked_for = (handle, kind, header_size)
        if self.raw:
            ctypes.memmove(buffer, self.raw, len(self.raw))
        return self.result


def raw_bytes(dx: int, dy: int, *, kind: int = 0) -> bytes:
    """A real RAWINPUT blob carrying the given mouse deltas."""
    raw = rawlook.RAWINPUT()
    raw.header.dwType = kind
    raw.mouse.lLastX = dx
    raw.mouse.lLastY = dy
    return bytes(raw)


def message_for(raw_handle: int):
    """A real MSG plus its address, as Qt would hand it to the filter."""
    msg = wintypes.MSG()
    msg.message = WM_INPUT
    msg.lParam = raw_handle
    return msg, ctypes.addressof(msg)


@pytest.fixture
def look(monkeypatch):
    """A collector that is "registered" without touching the real OS."""
    fake = FakeUser32(raw_bytes(0, 0))
    monkeypatch.setattr(rawlook, "_user32", fake)
    collector = RawMouseLook()
    collector._hwnd = 1  # as if start() had registered a window
    return collector, fake


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #


def test_rawinput_layout_matches_the_win32_offsets():
    """lLastX/lLastY sit at 36/40 in a 48-byte RAWINPUT on x64."""
    assert ctypes.sizeof(rawlook.RAWINPUT) == 48
    assert rawlook.RAWINPUT.mouse.offset + rawlook.RAWMOUSE.lLastX.offset == 36
    assert rawlook.RAWINPUT.mouse.offset + rawlook.RAWMOUSE.lLastY.offset == 40
    assert ctypes.sizeof(rawlook.RAWINPUTDEVICE) == 16


def test_button_state_does_not_shift_the_delta_fields():
    """The button union sits before the deltas; it must not overlap them."""
    raw = rawlook.RAWINPUT()
    raw.mouse.u.buttons.usButtonFlags = 0x0001
    raw.mouse.lLastX = 12
    raw.mouse.lLastY = -8

    parsed = rawlook.RAWINPUT.from_buffer_copy(bytes(raw))

    assert (parsed.mouse.lLastX, parsed.mouse.lLastY) == (12, -8)
    assert parsed.mouse.u.buttons.usButtonFlags == 0x0001


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_wm_input_is_parsed_into_mouse_deltas(look):
    collector, fake = look
    fake.raw = raw_bytes(7, -3)
    msg, address = message_for(RAW_HANDLE)

    handled = collector.nativeEventFilter(b"windows_generic_MSG", address)

    assert handled == (False, 0), "our filter must never swallow the message"
    assert collector.take() == (7, -3)
    assert fake.asked_for[0] == RAW_HANDLE, "asked about the posted handle"
    assert collector.take() == (0, 0), "reading the deltas clears them"


def test_deltas_accumulate_between_reads(look):
    collector, fake = look
    msg, address = message_for(RAW_HANDLE)

    for dx, dy in ((1, 2), (3, 4), (-5, -6)):
        fake.raw = raw_bytes(dx, dy)
        collector.nativeEventFilter(b"windows_generic_MSG", address)

    assert collector.take() == (-1, 0), "the batches add up"


def test_other_message_types_are_not_raw_input(look):
    collector, fake = look
    msg, address = message_for(RAW_HANDLE)
    msg.message = 0x0201  # WM_LBUTTONDOWN

    collector.nativeEventFilter(b"windows_generic_MSG", address)

    assert collector.take() == (0, 0)


def test_non_mouse_raw_input_is_ignored(look):
    """A keyboard/controller RAWINPUT has no mouse deltas to add."""
    collector, fake = look
    fake.raw = raw_bytes(50, 50, kind=1)  # RIM_TYPEKEYBOARD
    msg, address = message_for(RAW_HANDLE)

    collector.nativeEventFilter(b"windows_generic_MSG", address)

    assert collector.take() == (0, 0)


def test_a_failed_read_is_ignored(look):
    collector, fake = look
    fake.raw = raw_bytes(9, 9)
    fake.result = 0xFFFFFFFF  # what GetRawInputData returns on error
    msg, address = message_for(RAW_HANDLE)

    collector.nativeEventFilter(b"windows_generic_MSG", address)

    assert collector.take() == (0, 0)


def test_a_truncated_read_is_ignored(look):
    collector, fake = look
    fake.raw = raw_bytes(9, 9)
    fake.result = 4  # shorter than RAWINPUTHEADER
    msg, address = message_for(RAW_HANDLE)

    collector.nativeEventFilter(b"windows_generic_MSG", address)

    assert collector.take() == (0, 0)


def test_filter_is_inert_until_a_window_is_registered(monkeypatch):
    """Stopping must not leave a stale collector drinking mouse movement."""
    fake = FakeUser32(raw_bytes(11, 11))
    monkeypatch.setattr(rawlook, "_user32", fake)
    collector = RawMouseLook()
    msg, address = message_for(RAW_HANDLE)

    assert not collector.active
    assert collector.nativeEventFilter(b"windows_generic_MSG", address) == (False, 0)
    assert collector.take() == (0, 0)

    collector._hwnd = 1
    collector.nativeEventFilter(b"windows_generic_MSG", address)
    assert collector.take() == (11, 11), "and works once registered"


def test_other_platform_events_are_ignored(look):
    collector, fake = look
    fake.raw = raw_bytes(4, 4)
    msg, address = message_for(RAW_HANDLE)

    collector.nativeEventFilter(b"xcb_generic_event_t", address)

    assert collector.take() == (0, 0)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_start_refuses_without_a_window_or_an_application(monkeypatch):
    collector = RawMouseLook()
    monkeypatch.setattr(rawlook, "_user32", FakeUser32(raw_bytes(0, 0)))

    assert collector.start(0) is False
    assert not collector.active
    assert collector.take() == (0, 0)


def test_stop_is_harmless_when_never_started(monkeypatch):
    monkeypatch.setattr(rawlook, "_user32", FakeUser32(raw_bytes(0, 0)))
    collector = RawMouseLook()

    collector.stop()  # no window, no complaint

    assert not collector.active
    assert not collector.start(0)
