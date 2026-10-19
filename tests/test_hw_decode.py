"""``--hw-decode``: mode resolution, decoder fallback, CLI wiring.

The pure parts run everywhere; decoder-opening tests only need PyAV (no GPU --
``auto`` falls back to software when no accelerator is available).
"""

from __future__ import annotations

import threading

import pytest

av = pytest.importorskip("av")

from droidmirror.decoder import (  # noqa: E402
    HW_ALL,
    HW_AUTO,
    HW_AUTO_ORDER,
    HW_OFF,
    MAX_CONSECUTIVE_ERRORS,
    SOFTWARE_BACKEND,
    VideoDecoder,
    available_hw_devices,
    resolve_hw_types,
)
from droidmirror.protocol import CodecId, MediaPacket  # noqa: E402


# --------------------------------------------------------------------------- #
# resolve_hw_types (pure)
# --------------------------------------------------------------------------- #


def test_off_yields_no_accelerators():
    assert resolve_hw_types(HW_OFF) == ()
    assert resolve_hw_types(None) == ()
    assert resolve_hw_types("") == ()


def test_explicit_device_is_always_attempted():
    assert resolve_hw_types("qsv") == ("qsv",)
    assert resolve_hw_types("d3d11va") == ("d3d11va",)


def test_auto_only_offers_measured_safe_devices():
    result = resolve_hw_types(HW_AUTO)
    assert set(result) <= set(available_hw_devices())
    assert set(result) <= set(HW_AUTO_ORDER)
    # d3d11va measured ~69% *slower* than software: never auto-selected.
    assert "d3d11va" not in result


def test_available_hw_devices_is_a_list():
    assert isinstance(available_hw_devices(), list)


def test_off_is_the_default_mode():
    decoder = VideoDecoder(CodecId.H264)
    try:
        assert decoder.backend == SOFTWARE_BACKEND
        assert not decoder.uses_hardware
    finally:
        decoder.close()


# --------------------------------------------------------------------------- #
# Opening decoders: every mode must yield a working decoder
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", [HW_OFF, HW_AUTO, "d3d11va", "cuda"])
def test_every_mode_falls_back_instead_of_raising(mode):
    """A GPU that refuses to open must not stop the stream."""
    decoder = VideoDecoder(CodecId.H264, hw_decode=mode)
    try:
        assert decoder.backend == SOFTWARE_BACKEND or decoder.backend in HW_ALL
        # Whatever backend we ended on, decoding must work.
        frames = decoder.feed(MediaPacket(b"\x00\x00\x00\x01\x65garbage", 0, False, True))
        assert isinstance(frames, list)
        assert decoder.consecutive_errors >= 0
    finally:
        decoder.close()


def test_reset_keeps_the_chosen_backend():
    decoder = VideoDecoder(CodecId.H264, hw_decode=HW_OFF)
    try:
        backend = decoder.backend
        decoder.reset()
        assert decoder.backend == backend
        assert decoder.flush() == []
    finally:
        decoder.close()


# --------------------------------------------------------------------------- #
# Decoder resilience
# --------------------------------------------------------------------------- #


def test_sustained_decode_errors_do_not_deadlock(monkeypatch):
    """Past the error threshold the decoder resets itself -- it must not hang.

    Regression test. feed() holds self._lock while counting consecutive
    errors and, once past MAX_CONSECUTIVE_ERRORS, calls reset(), which takes
    the same lock again on the same thread. With a plain, non-reentrant
    threading.Lock that is a self-deadlock: the reader thread wedges forever
    holding the lock, and nothing is ever reported -- on precisely the corrupt
    stream the recovery exists to handle.

    Drives that path with a decoder whose context always raises. If the lock
    is not reentrant the worker never finishes and this fails on the timeout.
    """
    decoder = VideoDecoder(CodecId.H264, hw_decode=HW_OFF)

    class AlwaysFails:
        def decode(self, _packet=None, *_a, **_k):
            # FFmpegError takes (code, message), not just a message.
            raise av.FFmpegError(0, "simulated corrupt stream")

    monkeypatch.setattr(decoder, "_ctx", AlwaysFails(), raising=False)

    done = threading.Event()

    def hammer() -> None:
        # Comfortably past the threshold, so reset() is reached repeatedly.
        for _ in range(MAX_CONSECUTIVE_ERRORS * 3):
            decoder.feed(MediaPacket(b"\x00" * 64, 0, False, False))
        done.set()

    worker = threading.Thread(target=hammer, daemon=True)
    worker.start()
    worker.join(timeout=10.0)

    assert done.is_set(), (
        "decoder.feed() hung past the consecutive-error threshold -- "
        "_lock is not reentrant"
    )


def test_conversion_failures_are_counted():
    """A frame that cannot be turned into planes must be visible, not silent.

    Every such failure used to return None with no counter, no status message
    and no log entry, which turned a broken accelerator into a black screen
    with no way to tell why.
    """
    before = VideoDecoder.conversion_failures

    class NoSize:
        width = 0
        height = 0

    assert VideoDecoder._convert(NoSize()) is None
    assert VideoDecoder.conversion_failures == before + 1


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #


def test_hw_decode_defaults_to_off():
    from droidmirror.__main__ import build_parser

    assert build_parser().parse_args([]).hw_decode == HW_OFF


@pytest.mark.parametrize("mode", ["off", "auto", "amf", "qsv", "dxva2", "d3d11va"])
def test_hw_decode_cli_accepts_every_documented_mode(mode):
    from droidmirror.__main__ import build_parser

    assert build_parser().parse_args(["--hw-decode", mode]).hw_decode == mode


def test_hw_decode_cli_rejects_junk():
    from droidmirror.__main__ import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--hw-decode", "voodoo"])
