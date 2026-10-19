"""Tests for the rolling FPS counter.

The motivating bug: a session-wide ``total_frames / total_seconds`` average
looks fine in a test but lies in use, because scrcpy sends *no* frames while
the screen is static. Idle time gets averaged into the number, so the status
bar reads ~30fps while the pipeline is actually running much faster under load.
"""

from __future__ import annotations

import pytest

from droidmirror.session import FpsCounter


def test_reports_a_stable_rate_over_consecutive_windows():
    c = FpsCounter(window_s=1.0)
    assert c.sample(0, now=0.0) == 0.0          # first sample only anchors
    assert c.sample(60, now=1.0) == 60.0
    assert c.sample(120, now=2.0) == 60.0
    assert c.sample(180, now=3.0) == 60.0


def test_keeps_the_anchor_until_a_window_elapses():
    c = FpsCounter(window_s=1.0)
    c.sample(0, now=0.0)
    # Inside the window: keep the previous reading, do not re-anchor.
    assert c.sample(10, now=0.1) == 0.0
    # The measurement period therefore still starts at 0.0.
    assert c.sample(70, now=1.1) == pytest.approx(70 / 1.1)
    # Next complete window begins where the last one ended.
    assert c.sample(130, now=2.1) == pytest.approx(60.0)


def test_does_not_let_idle_time_dilute_the_reading():
    """The exact scenario the old cumulative average got wrong."""
    c = FpsCounter(window_s=1.0)

    # 10 seconds of motion at a solid 60fps.
    c.sample(0, now=0.0)
    frames = 0
    for second in range(1, 11):
        frames += 60
        rate = c.sample(frames, now=float(second))
    assert rate == 60.0

    # The screen then goes static for a minute. The counter should drop toward
    # zero (honest: scrcpy genuinely sends nothing), but it must never
    # re-average the earlier 600 frames into a bogus mid-range number.
    c.sample(frames, now=70.0)
    assert c.rate < 5.0
    # The old metric would have reported 600/70 = 8.6fps throughout.
    assert abs(600 / 70 - 8.6) < 0.1


def test_reanchors_after_a_stream_restart():
    c = FpsCounter(window_s=1.0)
    c.sample(0, now=0.0)
    assert c.sample(60, now=1.0) == 60.0
    # Counter goes backwards on reconnect -- must not report a negative rate.
    assert c.sample(3, now=5.0) >= 0.0
    assert c.sample(63, now=6.0) == 60.0


def test_custom_window():
    c = FpsCounter(window_s=0.5)
    c.sample(0, now=0.0)
    assert c.sample(30, now=0.5) == 60.0


def test_reset_clears_rate():
    c = FpsCounter(window_s=1.0)
    c.sample(0, now=0.0)
    c.sample(60, now=1.0)
    assert c.rate == 60.0
    c.reset()
    assert c.rate == 0.0
    # After reset the next sample re-anchors rather than extrapolating.
    assert c.sample(100, now=1.1) == 0.0
