"""End-to-end video pipeline test, entirely offline.

Generates real H.264 with ffmpeg, slices it into access units, wraps each one in
the exact scrcpy wire framing, then pushes it through ``VideoDemuxer`` and
``VideoDecoder`` and checks that real, correctly-laid-out frames come out.

This is the closest we can get to validating the receive path without a phone.
"""

from __future__ import annotations

import shutil
import struct
import subprocess

import numpy as np
import pytest

av = pytest.importorskip("av")

from droidmirror.decoder import VideoDecoder, resolve_hw_types  # noqa: E402
from droidmirror.protocol import (  # noqa: E402
    CodecId,
    MediaPacket,
    PACKET_FLAG_CONFIG,
    PACKET_FLAG_KEY_FRAME,
    PACKET_FLAG_SESSION,
    SessionInfo,
    VideoDemuxer,
)

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not installed")

WIDTH, HEIGHT = 320, 240


def _encode(out_path, lavfi_source: str, pix_fmt: str | None = "yuv420p") -> bytes:
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", lavfi_source,
        "-t", "1",
    ]
    if pix_fmt:
        cmd += ["-pix_fmt", pix_fmt]
    cmd += [
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-bsf:v", "h264_mp4toannexb", "-f", "h264", str(out_path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out_path.read_bytes()


@pytest.fixture(scope="module")
def h264_stream(tmp_path_factory) -> bytes:
    return _encode(
        tmp_path_factory.mktemp("vid") / "testsrc.h264",
        f"testsrc=size={WIDTH}x{HEIGHT}:rate=10",
    )


@pytest.fixture(scope="module")
def solid_stream(tmp_path_factory) -> bytes:
    """A flat colour clip: chroma is constant, so U/V mixups are obvious."""
    return _encode(
        tmp_path_factory.mktemp("vid") / "solid.h264",
        f"color=c=orange:size={WIDTH}x{HEIGHT}:rate=10",
    )


def to_access_units(stream: bytes) -> list[bytes]:
    parser = av.CodecContext.create("h264", "r")
    return [bytes(p) for p in parser.parse(stream)]


def wrap_scrcpy(units: list[bytes]) -> bytes:
    """Frame access units exactly as Streamer.java would."""
    buf = bytearray(struct.pack(">I", int(CodecId.H264)))
    buf += struct.pack(">III", PACKET_FLAG_SESSION >> 32, WIDTH, HEIGHT)
    pts = 0
    for i, unit in enumerate(units):
        config = i == 0  # MediaCodec emits SPS/PPS first, as a config packet
        key = i <= 1
        if config:
            flags = PACKET_FLAG_CONFIG
        else:
            flags = pts
            if key:
                flags |= PACKET_FLAG_KEY_FRAME
            pts += 100_000
        buf += struct.pack(">QI", flags, len(unit))
        buf += unit
    return bytes(buf)


def decode_units(units: list[bytes]):
    events = VideoDemuxer().feed(wrap_scrcpy(units))
    assert events[0] == SessionInfo(WIDTH, HEIGHT)
    packets = [e for e in events if isinstance(e, MediaPacket)]
    assert len(packets) == len(units)
    assert packets[0].config is True
    assert packets[1].key_frame is True

    decoder = VideoDecoder(CodecId.H264)
    frames = []
    for packet in packets:
        frames.extend(decoder.feed(packet))
    # Frame threading parks the tail of the stream in its pipeline; a drain
    # (what a live stream gets from the next packet) hands it over.
    frames.extend(decoder.flush())
    return frames, packets


def reference_frames(stream: bytes) -> list:
    ctx = av.CodecContext.create("h264", "r")
    out = []
    for p in ctx.parse(stream):
        out.extend(ctx.decode(p))
    out.extend(ctx.decode(None))  # same drain, so both sides end on one frame
    return out


# The conversion under test is the one shipped by the package, not a copy.
from droidmirror.color import yuv420_to_rgb  # noqa: E402


# --------------------------------------------------------------------------- #


def test_real_h264_survives_the_whole_pipeline(h264_stream):
    units = to_access_units(h264_stream)
    assert len(units) >= 8

    frames, packets = decode_units(units)

    assert frames, "decoder produced no frames"
    first = frames[0]
    assert (first.width, first.height) == (WIDTH, HEIGHT)
    assert first.y.shape == (HEIGHT, WIDTH)
    assert first.u.shape == (HEIGHT // 2, WIDTH // 2)
    assert first.v.shape == (HEIGHT // 2, WIDTH // 2)
    assert first.y.dtype == np.uint8


def test_planes_are_owned_and_contiguous(h264_stream):
    """Planes must not alias frame-owned memory, or they dangle after GC."""
    frames, _ = decode_units(to_access_units(h264_stream))
    for plane in frames[0].planes:
        assert plane.flags["C_CONTIGUOUS"]
        assert plane.flags["OWNDATA"]


def test_planes_match_the_decoders_own_buffers(h264_stream):
    """Byte-exact check that we copy the planes out with the right stride.

    ``frame.planes`` buffers are padded (a 320px plane has a 384px line size),
    so wrong stride handling would show up immediately here.
    """
    frames, _ = decode_units(to_access_units(h264_stream))
    ref = reference_frames(h264_stream)[-1].reformat(format="yuv420p")

    for i, got in enumerate(frames[-1].planes):
        p = ref.planes[i]
        assert p.line_size > p.width, "expected padding; stride logic untested"
        buf = np.frombuffer(p, dtype=np.uint8, count=p.line_size * p.height)
        expected = buf.reshape(p.height, p.line_size)[:, : p.width]
        assert np.array_equal(got, expected)


def test_chroma_planes_reconstruct_the_right_colour(solid_stream):
    """A flat orange frame must come back orange -- this catches a U/V swap."""
    frames, _ = decode_units(to_access_units(solid_stream))
    frame = frames[-1]

    # The source is a single colour, so every chroma sample is identical.
    assert np.all(frame.u == frame.u[0, 0])
    assert np.all(frame.v == frame.v[0, 0])

    got = yuv420_to_rgb(frame.y, frame.u, frame.v)
    ref = reference_frames(solid_stream)[-1].to_ndarray(format="rgb24")

    # Solid colour + no resampling error -> near-exact match.
    assert np.abs(got.astype(int) - ref.astype(int)).mean() < 3.0

    # And it really is orange: red channel dominant.
    r, g, b = got.mean(axis=(0, 1))
    assert r > g > b
    assert r > 200


def test_non_420_decoder_output_is_converted(tmp_path):
    """Some devices decode to other chroma formats; we must still get 420.

    Without forcing ``-pix_fmt`` this x264 build emits yuv444p, which exercises
    the reformat path rather than a no-op."""
    stream = _encode(tmp_path / "srgb.h264", f"testsrc=size={WIDTH}x{HEIGHT}:rate=10", None)
    frames, _ = decode_units(to_access_units(stream))
    assert frames
    frame = frames[-1]
    assert frame.y.shape == (HEIGHT, WIDTH)
    assert frame.u.shape == (HEIGHT // 2, WIDTH // 2)
    assert frame.v.shape == (HEIGHT // 2, WIDTH // 2)

    # And it still reconstructs plausible colour.
    rgb = yuv420_to_rgb(frame.y, frame.u, frame.v)
    assert rgb.shape == (HEIGHT, WIDTH, 3)
    assert rgb.std() > 10  # not a flat/blank image


def test_flush_and_reset_are_safe():
    decoder = VideoDecoder(CodecId.H264)
    assert decoder.flush() == []
    decoder.reset()  # must not raise
    assert decoder.feed(MediaPacket(b"", 0, False, False)) == []
    decoder.close()


def test_chroma_is_half_resolution(h264_stream):
    frames, _ = decode_units(to_access_units(h264_stream))
    f = frames[-1]
    assert f.chroma_width == f.width // 2
    assert f.chroma_height == f.height // 2


def test_packed_buffer_is_self_consistent(h264_stream):
    frames, _ = decode_units(to_access_units(h264_stream))
    f = frames[-1]
    packed = f.packed()
    assert packed.shape == (f.height + f.chroma_height, f.width)
    assert np.array_equal(packed[: f.height], f.y)
    ch, cw = f.chroma_height, f.chroma_width
    assert np.array_equal(packed[f.height : f.height + ch, :cw], f.u)
    assert np.array_equal(packed[f.height : f.height + ch, cw : cw * 2], f.v)
    assert packed.flags["C_CONTIGUOUS"]


def test_garbage_packets_do_not_crash_the_decoder():
    """Corrupt input must be swallowed, not propagated to the UI thread."""
    decoder = VideoDecoder(CodecId.H264)
    for bad in (b"\x00\x00\x00\x01\x65garbagegarbage", b"", b"\xff" * 64):
        decoder.feed(MediaPacket(bad, 0, False, False))
    assert decoder.consecutive_errors >= 0


def test_frames_carry_latency_timestamps(h264_stream):
    """Every decoded frame must be stamped for the latency HUD."""
    frames, packets = decode_units(to_access_units(h264_stream))
    assert frames

    packet_pts = {p.pts for p in packets}
    for frame in frames:
        assert frame.recv_ns > 0, "missing arrival timestamp"
        assert frame.decoded_ns >= frame.recv_ns
        assert frame.decode_ms >= 0.0
        assert frame.pts_us in packet_pts, "pts must come from the wire header"

    # feed() is called sequentially, so arrival stamps never go backwards.
    arrivals = [f.recv_ns for f in frames]
    assert arrivals == sorted(arrivals)


def test_auto_hw_decode_decodes_the_stream(h264_stream):
    """When an accelerator exists it must actually produce frames -- a silent
    fallback (or a hardware context that never emits) would defeat the point."""
    if not resolve_hw_types("auto"):
        pytest.skip("no hardware decoder available on this host")

    decoder = VideoDecoder(CodecId.H264, hw_decode="auto")
    try:
        frames = []
        for packet in decode_units(to_access_units(h264_stream))[1]:
            frames.extend(decoder.feed(packet))
        assert frames, f"{decoder.backend} produced no frames"
        assert decoder.uses_hardware
        assert frames[-1].y.shape == (HEIGHT, WIDTH)
        # The download-from-GPU conversion must yield sane pixels.
        assert frames[-1].y.mean() > 0
    finally:
        decoder.close()


def test_pipeline_survives_odd_chunking(h264_stream):
    """The demuxer must reassemble a real stream fed 7 bytes at a time."""
    wrapped = wrap_scrcpy(to_access_units(h264_stream))
    demuxer = VideoDemuxer()
    events = []
    for i in range(0, len(wrapped), 7):
        events.extend(demuxer.feed(wrapped[i : i + 7]))
    decoder = VideoDecoder(CodecId.H264)
    frames = []
    for e in events:
        if isinstance(e, MediaPacket):
            frames.extend(decoder.feed(e))
    assert frames
    assert frames[-1].y.shape == (HEIGHT, WIDTH)
