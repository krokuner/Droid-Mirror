"""Audio path: demuxer framing, PCM buffering, and end-to-end streaming.

The streaming test builds real opus packets (the PyAV wheels bundle the
encoder), frames them exactly like scrcpy does, and pushes them through
AudioStream over a socketpair with a fake sink -- no device, no speakers.
"""

from __future__ import annotations

import socket
import struct
import threading
import time

import pytest

from droidmirror.audio import (
    AudioStream,
    PcmBuffer,
    decode_packet,
    make_decoder,
)
from droidmirror.protocol import (
    PACKET_FLAG_CONFIG,
    PACKET_FLAG_KEY_FRAME,
    AudioCodecId,
    AudioDemuxer,
    ProtocolError,
)

OPUS_ID = struct.pack(">I", int(AudioCodecId.OPUS))
PTS_MASK = (1 << 61) - 1


def audio_frame(pts: int, payload: bytes, *, config: bool = False, key: bool = False) -> bytes:
    """One scrcpy frame: 8-byte pts-and-flags, 4-byte size, payload."""
    flags = pts & PTS_MASK
    if config:
        flags = PACKET_FLAG_CONFIG
    if key:
        flags |= PACKET_FLAG_KEY_FRAME
    return struct.pack(">Q", flags) + struct.pack(">I", len(payload)) + payload


# --------------------------------------------------------------------------- #
# Demuxer
# --------------------------------------------------------------------------- #


def test_demuxer_reads_the_opus_codec_id():
    demuxer = AudioDemuxer()
    assert demuxer.feed(OPUS_ID) == []
    assert demuxer.codec == AudioCodecId.OPUS


def test_demuxer_parses_a_media_packet():
    demuxer = AudioDemuxer()
    payload = b"\x01\x02\x03\x04"
    events = demuxer.feed(OPUS_ID + audio_frame(123_456, payload, key=True))
    assert len(events) == 1
    packet = events[0]
    assert packet.data == payload
    assert packet.pts == 123_456
    assert packet.config is False
    assert packet.key_frame is True


def test_demuxer_config_packet_carries_no_pts():
    """Config packets set bit 62 and nothing else (pts is discarded)."""
    demuxer = AudioDemuxer()
    opus_head = b"OpusHead" + b"\x00" * 11  # the 19-byte CSD scrcpy sends
    events = demuxer.feed(OPUS_ID + audio_frame(0, opus_head, config=True))
    assert len(events) == 1
    assert events[0].config is True
    assert events[0].pts == 0
    assert events[0].data == opus_head


def test_demuxer_survives_fragmented_feeds():
    """TCP chunk boundaries must not matter, byte by byte."""
    demuxer = AudioDemuxer()
    blob = OPUS_ID + audio_frame(7, b"abc") + audio_frame(9, b"defg")
    events = []
    for i in range(len(blob)):
        events.extend(demuxer.feed(blob[i : i + 1]))
    assert [(e.pts, e.data) for e in events] == [(7, b"abc"), (9, b"defg")]


def test_demuxer_disabled_stream_reports_the_code():
    demuxer = AudioDemuxer()
    assert demuxer.feed(struct.pack(">I", 0)) == []
    assert demuxer.codec == AudioCodecId.DISABLED
    assert demuxer.error_code == 0


def test_demuxer_fatal_error_code():
    demuxer = AudioDemuxer()
    demuxer.feed(struct.pack(">I", 1))
    assert demuxer.codec == AudioCodecId.ERROR
    assert demuxer.error_code == 1


def test_demuxer_rejects_unknown_codec_ids():
    demuxer = AudioDemuxer()
    with pytest.raises(ProtocolError, match="unknown audio codec"):
        demuxer.feed(struct.pack(">I", 0xDEADBEEF))


def test_demuxer_rejects_session_packets():
    """Session packets are video-only; one here means framing is broken."""
    demuxer = AudioDemuxer()
    header = struct.pack(">Q", 1 << 63) + struct.pack(">I", 4)
    with pytest.raises(ProtocolError, match="session packet"):
        demuxer.feed(OPUS_ID + header)


def test_demuxer_rejects_implausible_sizes():
    demuxer = AudioDemuxer()
    header = struct.pack(">Q", 5) + struct.pack(">I", 1 << 27)
    with pytest.raises(ProtocolError, match="implausible"):
        demuxer.feed(OPUS_ID + header)


# --------------------------------------------------------------------------- #
# PCM buffer
# --------------------------------------------------------------------------- #


def test_buffer_roundtrip():
    buffer = PcmBuffer()
    buffer.write(b"\x01\x02\x03\x04")
    assert buffer.read(4) == b"\x01\x02\x03\x04"


def test_buffer_pads_underruns_with_silence():
    buffer = PcmBuffer()
    buffer.write(b"\x10\x20")
    out = buffer.read(6)
    assert out == b"\x10\x20\x00\x00\x00\x00"
    assert buffer.read(4) == b"\x00\x00\x00\x00"  # fully drained -> pure silence


def test_buffer_drops_oldest_when_over_capacity():
    """Pts jumps must resync playback to now, not bank a growing backlog."""
    buffer = PcmBuffer(max_seconds=0.01)  # 1920 bytes at 48k stereo int16
    buffer.write(b"\xaa" * 1000)
    buffer.write(b"\xbb" * 1500)
    assert buffer.buffered_bytes == 1500  # oldest chunk was dropped
    out = buffer.read(2000)
    assert out[:1500] == b"\xbb" * 1500
    assert out[1500:] == b"\x00" * 500


def test_buffer_clear():
    buffer = PcmBuffer()
    buffer.write(b"\xff" * 100)
    buffer.clear()
    assert buffer.buffered_bytes == 0
    assert buffer.read(4) == b"\x00" * 4


# --------------------------------------------------------------------------- #
# Codec round-trip
# --------------------------------------------------------------------------- #


def _make_opus_packets(count: int = 3) -> list[bytes]:
    av = pytest.importorskip("av")
    import numpy as np

    ctx = av.codec.CodecContext.create("opus", "w")
    ctx.sample_rate = 48_000
    ctx.layout = "stereo"
    ctx.format = "s16"  # libopus refuses to open without a concrete format
    ctx.bit_rate = 64_000
    ctx.open()
    packets = []
    for i in range(count):
        pcm = (np.sin(2 * np.pi * 440 * np.arange(960) / 48_000) * 12_000).astype(
            np.int16
        )
        frame = av.AudioFrame.from_ndarray(
            np.column_stack([pcm, pcm]).reshape(1, -1),
            format="s16",
            layout="stereo",
        )
        frame.sample_rate = 48_000
        frame.pts = i * 960
        packets.extend(bytes(p) for p in ctx.encode(frame))
    return packets


def test_opus_encode_decode_roundtrip_produces_loud_pcm():
    packets = _make_opus_packets()
    assert packets, "encoder produced no packets"

    ctx = make_decoder()
    pcm = b"".join(decode_packet(ctx, p) for p in packets)
    assert pcm, "decoder produced no PCM"
    assert len(pcm) % 4 == 0  # stereo int16 frames

    import numpy as np

    samples = np.frombuffer(pcm, dtype=np.int16)
    peak = int(np.abs(samples).max())
    assert peak > 1000, f"decoded audio is silent (peak={peak})"


# --------------------------------------------------------------------------- #
# Streaming over a socket
# --------------------------------------------------------------------------- #


def _pump(
    sock: socket.socket,
    *,
    codec_id: bytes = OPUS_ID,
    with_media: bool = True,
    close: bool = True,
) -> None:
    try:
        sock.sendall(codec_id)
        if with_media:
            sock.sendall(audio_frame(0, b"OpusHead\x00" * 2 + b"\x00", config=True))
            for pts, packet in enumerate(_make_opus_packets(4)):
                sock.sendall(audio_frame(pts * 20_000, packet))
    finally:
        if close:
            sock.close()


def test_audio_stream_decodes_and_sinks_pcm():
    left, right = socket.socketpair()
    sink_calls: list[bytes | None] = []
    statuses: list[tuple[str, str]] = []
    done = threading.Event()

    def sink(data: bytes | None) -> None:
        sink_calls.append(data)
        if data is None:
            done.set()

    stream = AudioStream(
        right, on_status=lambda m, l="info": statuses.append((m, l)), sink=sink
    )
    stream.start()
    _pump(left)

    assert done.wait(5.0), "audio thread never finished"
    stream.join(timeout=2.0)

    pcm_calls = [c for c in sink_calls if c]
    assert pcm_calls, "no PCM reached the sink"
    assert all(len(c) % 4 == 0 for c in pcm_calls)
    assert sink_calls[-1] is None  # shutdown hook always fires
    assert stream.codec == AudioCodecId.OPUS
    assert stream.packets_decoded >= 1


def test_muting_drops_the_already_decoded_audio():
    """Buffered PCM must go too: otherwise un-muting replays a stale backlog."""
    left, right = socket.socketpair()
    stream = AudioStream(right, sink=lambda data: None, mute=False)

    stream._buffer.write(b"\x01\x02\x03\x04")
    assert stream._buffer.buffered_bytes == 4

    stream.set_muted(True)

    assert stream.muted
    assert stream._buffer.buffered_bytes == 0, "the backlog is discarded"

    stream.set_muted(False)

    assert not stream.muted
    left.close()
    right.close()


def test_a_muted_stream_plays_nothing_and_un_muting_restores_it():
    left, right = socket.socketpair()
    sink_calls: list[bytes | None] = []
    done = threading.Event()

    def sink(data: bytes | None) -> None:
        sink_calls.append(data)
        if data is None:
            done.set()

    stream = AudioStream(right, sink=sink, mute=True)
    stream.start()

    _pump(left, close=False)  # real audio arrives while muted
    time.sleep(0.4)

    assert [c for c in sink_calls if c] == [], "muted: nothing is played"

    stream.set_muted(False)
    _pump(left, codec_id=b"")  # the same packets are already in flight

    assert done.wait(5.0), "the audio thread never finished"
    stream.join(timeout=2.0)

    assert [c for c in sink_calls if c], "un-muting restores playback"
    assert sink_calls[-1] is None


def test_session_mute_reaches_the_live_stream_and_sticks():
    """The toolbar toggle must also silence the *next* connection."""
    from droidmirror.session import MirrorSession

    session = MirrorSession(object())

    class Recorder:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def set_muted(self, muted: bool) -> None:
            self.calls.append(muted)

    live = Recorder()
    session.audio = live

    session.set_muted(True)

    assert live.calls == [True]
    assert session.muted is True, "remembered for the next AudioStream too"

    session.audio = None  # disconnected: the choice must survive
    session.set_muted(False)
    assert session.muted is False


def test_audio_stream_stops_cleanly_when_capture_is_disabled():
    left, right = socket.socketpair()
    sink_calls: list[bytes | None] = []
    statuses: list[tuple[str, str]] = []

    stream = AudioStream(
        right, on_status=lambda m, l="info": statuses.append((m, l)),
        sink=lambda d: sink_calls.append(d),
    )
    stream.start()
    left.sendall(struct.pack(">I", 0))  # device: audio unavailable
    left.close()

    stream.join(timeout=5.0)
    assert not stream.is_alive()
    assert any("unavailable" in m for m, _ in statuses)
    assert sink_calls == [None]  # never opened a stream, clean shutdown


def test_audio_stream_unknown_codec_reports_and_exits():
    left, right = socket.socketpair()
    statuses: list[tuple[str, str]] = []
    sink_calls: list[bytes | None] = []

    stream = AudioStream(
        right, on_status=lambda m, l="info": statuses.append((m, l)),
        sink=lambda d: sink_calls.append(d),
    )
    stream.start()
    left.sendall(struct.pack(">I", int(AudioCodecId.AAC)))
    left.close()

    stream.join(timeout=5.0)
    assert not stream.is_alive()
    assert any("not supported" in m for m, _ in statuses)
    assert sink_calls == [None]


def test_audio_stream_stop_wakes_a_blocked_reader():
    left, right = socket.socketpair()
    stream = AudioStream(right, sink=lambda d: None)
    stream.start()
    time.sleep(0.1)
    assert stream.is_alive()  # parked in recv()

    stream.stop()
    stream.join(timeout=3.0)
    assert not stream.is_alive()
    left.close()


def test_audio_stream_exits_after_consecutive_decode_errors(monkeypatch):
    from droidmirror import audio as audio_mod

    def always_fail(_decoder, _data):
        raise RuntimeError("corrupt opus frame")

    monkeypatch.setattr(audio_mod, "decode_packet", always_fail)

    left, right = socket.socketpair()
    statuses: list[tuple[str, str]] = []
    stream = AudioStream(
        right,
        on_status=lambda m, l="info": statuses.append((m, l)),
        sink=lambda _d: None,
    )
    stream.start()
    try:
        left.sendall(OPUS_ID)
        for i in range(audio_mod.MAX_CONSECUTIVE_DECODE_ERRORS + 5):
            left.sendall(audio_frame(i * 20_000, b"bad"))
            time.sleep(0.005)
        stream.join(timeout=3.0)
        assert not stream.is_alive(), "outer receive loop must exit on fatal decode errors"
        error_msgs = [m for m, lvl in statuses if lvl == "error"]
        assert len(error_msgs) == 1
    finally:
        left.close()
        right.close()
