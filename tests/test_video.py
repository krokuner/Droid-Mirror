"""Video socket demuxer tests.

Builds synthetic streams in exactly the layout the v4.1 server writes and
checks the demuxer reassembles them, including under pathological chunking.
"""

from __future__ import annotations

import random
import struct

import pytest

from droidmirror.protocol import (
    DEVICE_NAME_FIELD_LENGTH,
    CodecId,
    MediaPacket,
    PACKET_FLAG_CONFIG,
    PACKET_FLAG_KEY_FRAME,
    PACKET_FLAG_SESSION,
    ProtocolError,
    SessionInfo,
    VideoDemuxer,
    decode_device_name,
)


def session_header(width: int, height: int, client_resized: bool = False) -> bytes:
    """Mirrors Streamer.writeSessionMeta()."""
    flags = PACKET_FLAG_SESSION >> 32
    if client_resized:
        flags |= 1
    return struct.pack(">III", flags, width, height)


def frame_header(pts: int, size: int, config: bool, key: bool) -> bytes:
    """Mirrors Streamer.writeFrameMeta()."""
    if config:
        pts_and_flags = PACKET_FLAG_CONFIG
    else:
        pts_and_flags = pts
        if key:
            pts_and_flags |= PACKET_FLAG_KEY_FRAME
    return struct.pack(">QI", pts_and_flags, size)


def build_stream(packets, width=1080, height=1920) -> bytes:
    out = bytearray(struct.pack(">I", int(CodecId.H264)))
    out += session_header(width, height)
    for pkt in packets:
        out += frame_header(pkt["pts"], len(pkt["data"]), pkt["config"], pkt["key"])
        out += pkt["data"]
    return bytes(out)


CONFIG = {"pts": 0, "config": True, "key": False, "data": b"\x00\x00\x00\x01\x67SPS"}
KEY = {"pts": 1000, "config": False, "key": True, "data": b"\x00\x00\x00\x01\x65IFRAME"}
P = {"pts": 2000, "config": False, "key": False, "data": b"\x00\x00\x00\x01\x41PFRAME"}


def test_parses_codec_session_and_packets():
    demuxer = VideoDemuxer()
    events = demuxer.feed(build_stream([CONFIG, KEY, P]))
    assert demuxer.codec == CodecId.H264
    assert events[0] == SessionInfo(1080, 1920, False)
    assert events[1] == MediaPacket(CONFIG["data"], 0, True, False)
    assert events[2] == MediaPacket(KEY["data"], 1000, False, True)
    assert events[3] == MediaPacket(P["data"], 2000, False, False)


def test_handles_arbitrary_chunk_boundaries():
    stream = build_stream([CONFIG, KEY, P, P, KEY])
    reference = VideoDemuxer().feed(stream)

    rng = random.Random(1234)
    demuxer = VideoDemuxer()
    events = []
    i = 0
    while i < len(stream):
        n = rng.randint(1, 17)
        events.extend(demuxer.feed(stream[i : i + n]))
        i += n
    assert events == reference


def test_byte_at_a_time():
    stream = build_stream([CONFIG, KEY])
    demuxer = VideoDemuxer()
    events = []
    for b in stream:
        events.extend(demuxer.feed(bytes([b])))
    assert len(events) == 3  # session + config + key


def test_session_packet_after_rotation():
    stream = (
        struct.pack(">I", int(CodecId.H264))
        + session_header(1080, 1920)
        + frame_header(10, len(KEY["data"]), False, True)
        + KEY["data"]
        + session_header(1920, 1080)  # device rotated
        + frame_header(20, len(KEY["data"]), False, True)
        + KEY["data"]
    )
    events = VideoDemuxer().feed(stream)
    sessions = [e for e in events if isinstance(e, SessionInfo)]
    assert sessions == [SessionInfo(1080, 1920), SessionInfo(1920, 1080)]


def test_client_resized_flag_is_reported():
    stream = (
        struct.pack(">I", int(CodecId.H264))
        + session_header(800, 600, client_resized=True)
    )
    (event,) = VideoDemuxer().feed(stream)
    assert event == SessionInfo(800, 600, client_resized=True)


def test_codec_ids_roundtrip():
    for codec in (CodecId.H264, CodecId.H265, CodecId.AV1, CodecId.VP8, CodecId.VP9):
        d = VideoDemuxer()
        events = d.feed(struct.pack(">I", int(codec)) + session_header(2, 2))
        assert d.codec == codec
        assert events == [SessionInfo(2, 2)]


def test_disabled_and_error_streams_are_detected():
    for codec in (CodecId.DISABLED, CodecId.ERROR):
        d = VideoDemuxer()
        events = d.feed(struct.pack(">I", int(codec)))
        assert d.codec == codec
        assert d.error_code == int(codec)
        assert events == []


def test_unknown_codec_raises():
    with pytest.raises(ProtocolError):
        VideoDemuxer().feed(struct.pack(">I", 0xDEADBEEF))


def test_absurd_packet_size_raises():
    bad = (
        struct.pack(">I", int(CodecId.H264))
        + session_header(1, 1)
        + frame_header(0, 1 << 28, False, False)
    )
    with pytest.raises(ProtocolError):
        VideoDemuxer().feed(bad)


def test_large_pts_is_not_mistaken_for_a_flag():
    big_pts = (1 << 61) - 1
    stream = (
        struct.pack(">I", int(CodecId.H264))
        + session_header(1, 1)
        + frame_header(big_pts, len(P["data"]), False, False)
        + P["data"]
    )
    events = VideoDemuxer().feed(stream)
    assert events[1].pts == big_pts
    assert events[1].config is False


def test_max_pts_with_keyframe_flag_kept_separate():
    demuxer = VideoDemuxer()
    payload = b"x" * 8
    stream = (
        struct.pack(">I", int(CodecId.H264))
        + session_header(1, 1)
        + frame_header((1 << 61) - 1, len(payload), False, True)
        + payload
    )
    events = demuxer.feed(stream)
    assert events[1].key_frame is True
    assert events[1].pts == (1 << 61) - 1


def test_decode_device_name():
    name = b"Pixel 7\x00" + b"\x00" * (DEVICE_NAME_FIELD_LENGTH - 8)
    assert decode_device_name(name) == "Pixel 7"


def test_decode_device_name_handles_full_field():
    raw = b"A" * DEVICE_NAME_FIELD_LENGTH
    assert decode_device_name(raw) == "A" * DEVICE_NAME_FIELD_LENGTH


def test_decode_device_name_rejects_short_field():
    with pytest.raises(ProtocolError):
        decode_device_name(b"short")
