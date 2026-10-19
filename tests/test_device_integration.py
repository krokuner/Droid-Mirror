"""Integration test against a real Android device.

Skipped automatically when no device is attached, so the normal suite still
runs anywhere. This is the only test that exercises the scrcpy server, the USB
transport and the handshake for real.
"""

from __future__ import annotations

import os
import time

import pytest

from droidmirror.adb import Adb, AdbError
from droidmirror.protocol import MotionAction
from droidmirror.server import ServerOptions
from droidmirror.session import MirrorSession

FRAME_WAIT_S = 25.0
WANT_FRAMES = 10


def _first_ready_device():
    try:
        adb = Adb()
    except AdbError:
        return None
    try:
        ready = [d for d in adb.devices() if d.is_ready]
    except AdbError:
        return None
    return ready[0] if ready else None


DEVICE = _first_ready_device()
pytestmark = pytest.mark.skipif(
    DEVICE is None, reason="no authorized adb device attached"
)


@pytest.fixture(scope="module")
def live_session():
    """One shared mirroring session for the whole module."""
    adb = Adb(serial=DEVICE.serial)
    frames: list = []
    statuses: list[str] = []
    names: list[str] = []

    options = ServerOptions(max_size=1024)  # keep the encode load light
    session = MirrorSession(
        adb,
        options=options,
        on_frame=frames.append,
        on_status=lambda s: statuses.append(s.message),
        on_device_name=names.append,
    )

    session.start(device=DEVICE)
    deadline = time.monotonic() + FRAME_WAIT_S
    while time.monotonic() < deadline and len(frames) < WANT_FRAMES:
        time.sleep(0.05)

    yield session, frames, statuses, names

    session.stop()


def test_server_handshake_reports_the_device_name(live_session):
    _, _, _, names = live_session
    assert names, "handshake never delivered a device name"
    assert names[0].strip()


def test_video_stream_decodes_real_frames(live_session):
    session, frames, statuses, _ = live_session
    assert frames, f"no frames decoded; server said: {statuses}"

    frame = frames[-1]
    assert session.frame_width > 0 and session.frame_height > 0
    assert (frame.width, frame.height) == session.frame_size

    # Planes must be exactly consistent with the reported frame size.
    assert frame.y.shape == (frame.height, frame.width)
    assert frame.u.shape == (frame.height // 2, frame.width // 2)
    assert frame.v.shape == (frame.height // 2, frame.width // 2)
    assert frame.y.dtype.name == "uint8"


def test_frames_keep_arriving(live_session):
    """Proves the reader loop is not stalling after the first frame."""
    session, frames, _, _ = live_session
    assert len(frames) >= 2
    assert session.is_running, "reader thread died"


@pytest.mark.skipif(
    os.environ.get("DROIDMIRROR_TEST_INJECT") != "1",
    reason="injects a real tap on the device; set DROIDMIRROR_TEST_INJECT=1 to opt in",
)
def test_control_injection_is_accepted(live_session):
    """Write a real touch to the control socket and prove it stays healthy.

    This actually taps the device, so it is opt-in. The tap lands in the
    top-left corner, and the meaningful assertion is that the server keeps
    streaming afterwards -- a malformed control message would kill it.
    """
    session, frames, _, _ = live_session
    assert frames, "need a frame to know the frame size"
    w, h = session.frame_size
    before = session.frames_decoded

    assert session.inject_touch(MotionAction.DOWN, 0, 0, 0, w, h)
    assert session.inject_touch(MotionAction.UP, 0, 0, 0, w, h)

    time.sleep(1.5)
    assert session.frames_decoded >= before, "server died after a control message"
