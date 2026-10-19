"""Lag measurement and catch-up drain.

The "mirror is N seconds behind" failure: the reader cannot keep pace with
the encoder, old video banks up in the transport buffers, and the mirror
drifts while control stays instant. These tests pin the two halves of the
fix -- measuring staleness against the device clock, and draining the
backlog without decoding until the stream is legally re-enterable.
"""

from __future__ import annotations

import pytest

from droidmirror.protocol import MediaPacket, SessionInfo
from droidmirror.session import LAG_CATCHUP_S, LagMeter, MirrorSession


# --------------------------------------------------------------------------- #
# LagMeter
# --------------------------------------------------------------------------- #


def test_first_packet_anchors_and_reports_live():
    meter = LagMeter()
    assert meter.feed(1_000_000, now=10.0) == 0.0
    # Wall and pts advancing together means no staleness.
    assert meter.feed(1_500_000, now=10.5) == pytest.approx(0.0)


def test_staleness_is_wall_minus_pts_advance():
    meter = LagMeter()
    meter.feed(1_000_000, now=10.0)
    # One wall second passed but the device clock only moved 0.1s: the
    # newest packet is 0.9s old.
    lag = meter.feed(1_100_000, now=11.0)
    assert lag == pytest.approx(0.9)
    assert meter.lag == pytest.approx(0.9)


def test_reanchors_when_pts_runs_backwards():
    """Encoder restarts hand back an older clock; that is not 5s of lag."""
    meter = LagMeter()
    meter.feed(5_000_000, now=10.0)
    lag = meter.feed(1_000_000, now=10.2)  # jumped backwards -> clock reset
    assert lag == 0.0
    # And measuring resumes from the new anchor.
    assert meter.feed(1_100_000, now=10.7) == pytest.approx(0.4)


def test_reanchors_on_absurdly_large_lag():
    """Device suspend can fake minutes of staleness; that is a clock offset,
    not something to drain a live stream over."""
    meter = LagMeter()
    meter.feed(1_000_000, now=10.0)
    lag = meter.feed(11_000_000, now=10.0 + 500.0)  # 490s wall, 10s pts
    assert lag == 0.0
    assert meter.lag == 0.0


def test_ptsless_packets_keep_the_last_reading():
    """Config packets carry no usable pts; they must not zero the meter."""
    meter = LagMeter()
    meter.feed(1_000_000, now=10.0)
    meter.feed(1_000_000, now=12.0)  # 2s of wall, no pts progress
    assert meter.lag == pytest.approx(2.0)
    assert meter.feed(0, now=13.0) == pytest.approx(2.0)
    assert meter.lag == pytest.approx(2.0)


def test_reset_forgets_the_anchor():
    meter = LagMeter()
    meter.feed(1_000_000, now=10.0)
    meter.feed(1_000_000, now=15.0)
    meter.reset()
    assert meter.lag == 0.0
    # Fresh anchor: a huge pts delta on the first packet reads as live.
    assert meter.feed(999_000_000, now=50.0) == 0.0


# --------------------------------------------------------------------------- #
# Catch-up drain
# --------------------------------------------------------------------------- #


class StubDecoder:
    """Stands in for VideoDecoder; records which packets reached decode."""

    def __init__(self) -> None:
        self.backend = "software"
        self.fed: list[MediaPacket] = []
        self.consecutive_errors = 0
        self.resets = 0

    def feed(self, packet: MediaPacket) -> list:
        self.fed.append(packet)
        return []

    def reset(self) -> None:
        self.resets += 1


class FixedLag:
    """LagMeter stand-in with a predetermined staleness."""

    def __init__(self, lag: float) -> None:
        self.lag = lag

    def feed(self, pts_us: int, now: float | None = None) -> float:
        return self.lag


def make_session(lag: float, statuses: list | None = None) -> MirrorSession:
    session = MirrorSession(None)
    session._decoder = StubDecoder()
    session._lag = FixedLag(lag)
    if statuses is not None:
        session._on_status = statuses.append
    return session


def packet(pts: int, *, config: bool = False, key: bool = False) -> MediaPacket:
    return MediaPacket(b"\x00\x00\x00\x01\x65x", pts, config, key)


def test_under_threshold_every_packet_decodes():
    statuses: list = []
    session = make_session(lag=1.0, statuses=statuses)
    for i in range(3):
        session._handle_event(packet(i * 100_000, key=i == 0))

    assert len(session._decoder.fed) == 3
    assert session.frames_dropped == 0
    assert session.video_lag_s == 1.0
    assert not session._draining
    assert statuses == []  # healthy stream says nothing


def test_over_threshold_drains_until_the_next_keyframe():
    statuses: list = []
    session = make_session(lag=LAG_CATCHUP_S + 2.0, statuses=statuses)

    # First stale packet opens the drain instead of decoding.
    session._handle_event(packet(0, key=True))
    assert session._decoder.fed == []
    assert session._draining
    assert any("behind" in s.message for s in statuses)

    # Mid-GOP frames reference the discarded backlog: skipped, not decoded.
    session._handle_event(packet(100_000))
    session._handle_event(packet(200_000))
    assert session._decoder.fed == []
    assert session.frames_dropped == 3  # opener + two skipped

    # An IDR is the only legal re-entry point: decoding resumes there.
    session._handle_event(packet(300_000, key=True))
    assert len(session._decoder.fed) == 1
    assert not session._draining
    assert session.frames_dropped == 3
    assert any("Caught up" in s.message for s in statuses)


def test_config_packet_also_ends_the_drain():
    """A stream restart delivers SPS/PPS first; that re-arms decoding too."""
    session = make_session(lag=LAG_CATCHUP_S + 1.0)
    session._handle_event(packet(0))
    assert session._draining

    session._handle_event(packet(0, config=True))
    assert not session._draining
    assert len(session._decoder.fed) == 1


def test_drain_survives_a_size_change_event():
    session = make_session(lag=LAG_CATCHUP_S + 1.0)
    session._handle_event(packet(0))
    assert session._draining

    # Rotation mid-drain must not kill the drain or the decoder.
    session._handle_event(SessionInfo(800, 600))
    assert session._draining
    assert (session.frame_width, session.frame_height) == (800, 600)


def test_lag_starts_unknown_and_is_stamped_on_every_packet():
    session = make_session(lag=0.5)
    assert session.video_lag_s == -1.0
    session._handle_event(packet(0))
    assert session.video_lag_s == 0.5


def test_events_before_a_decoder_exists_are_ignored():
    session = MirrorSession(None)
    session._lag = FixedLag(99.0)
    session._handle_event(packet(0, key=True))  # no decoder installed yet
    assert session._draining is False
    assert session.video_lag_s == -1.0


def test_same_size_session_info_still_resets_decoder():
    session = make_session(lag=0.1)
    session._handle_event(SessionInfo(1920, 1080))
    assert session._decoder.resets == 1

    # A 180-degree rotation or encoder restart at the same resolution must
    # still reset the decoder state.
    session._handle_event(SessionInfo(1920, 1080))
    assert session._decoder.resets == 2
