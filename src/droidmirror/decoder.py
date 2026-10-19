"""Low-latency H.264 decoding.

Design notes for the "no delay" requirement:

* One decoder instance is fed packets in arrival order. We never queue encoded
  frames -- dropping them would break the H.264 reference chain. Instead we
  drop *decoded* frames downstream by always rendering the newest one.
* Software decode runs **frame-threaded** (``FRAME``, 2 threads). Frame
  threading only ever holds ``thread_count`` frames of pipeline depth -- a
  bounded cost, never growing -- which newest-frame rendering handles fine,
  and it measured nearly 2x faster than slice threading on the real device
  stream (1742x1088, ~41fps, ``.tmp/live.h264``)::

      SLICE count=0   9.83 ms/frame   (~100 fps capacity)
      FRAME  count=2  5.46 ms/frame   (~183 fps capacity), first frame out
                                      at packet 1 (no added latency)
      FRAME  count=4  DEADLOCKS on this stream -- deliberately not used

  The old SLICE choice kept the reader just at the edge of the stream rate;
  once the GUI competed for the CPU the reader fell behind and seconds of
  video banked up in the TCP buffers (the "mirror is 10s behind" bug).
* Decoding is kept in-process (PyAV/FFmpeg) so there is no pipe copy or
  subprocess hand-off on the hot path.

Hardware acceleration
---------------------
Hardware decode is *available* but it is not a free win: the GPU must still
hand the frame back to the CPU before we can upload planes, and that transfer
often cancels out the fixed-function decode. Benchmarked on the actual device
stream (1742x1088 @ ~5.7 Mbps, median of 4 runs, ms/frame):

    amf 9.13   qsv 9.37   software 9.41   dxva2 9.80   d3d11va 15.93

So on this host hardware decode is within noise, and ``d3d11va`` is ~69%
*slower* than software. Enabling hardware blindly would therefore risk adding
latency rather than removing it, which is why the default is ``off`` and
``auto`` never selects ``d3d11va``. On a CPU too slow to sustain the stream,
hardware decode is a genuine win -- which is what the option is for.
Hardware contexts keep SLICE threading (FRAME+hw is unmeasured).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import av
import av.codec.hwaccel as _hwaccel
import numpy as np

from .protocol import CodecId, MediaPacket

#: Maps the wire codec id onto an FFmpeg decoder name.
_CODEC_NAMES = {
    CodecId.H264: "h264",
    CodecId.H265: "hevc",
    CodecId.AV1: "av1",
    CodecId.VP8: "vp8",
    CodecId.VP9: "vp9",
}

#: ``--hw-decode`` values.
HW_OFF = "off"
HW_AUTO = "auto"
HW_MODES = (HW_OFF, HW_AUTO)

#: What ``auto`` will try, in order. Measured fastest-first on this host;
#: ``d3d11va`` is deliberately absent because it measured ~69% *slower* than
#: software (it is still reachable via an explicit ``--hw-decode d3d11va``).
HW_AUTO_ORDER = ("amf", "qsv", "dxva2")

#: An ordering of every accelerator, used for the dropdown/HUD hints.
HW_ALL = ("amf", "qsv", "dxva2", "d3d11va", "cuda")

SOFTWARE_BACKEND = "software"

#: Software-decode threading: 2 frame threads measured ~2x faster than slice
#: threading on the real stream (see the module docstring). 4+ deadlocks.
DEFAULT_THREAD_TYPE = "FRAME"
DEFAULT_THREAD_COUNT = 2

#: Consecutive decode failures before the decoder is reset and the stream
#: resyncs from the next keyframe. A single dropped packet is survivable;
#: this many in a row means the stream is genuinely broken.
MAX_CONSECUTIVE_ERRORS = 60


def available_hw_devices() -> list[str]:
    """Hardware device types FFmpeg was built with on this machine."""
    try:
        return list(_hwaccel.hwdevices_available())
    except Exception:  # pragma: no cover - platform dependent
        return []


def resolve_hw_types(mode: str | None) -> tuple[str, ...]:
    """Turn a ``--hw-decode`` value into concrete device types to attempt.

    ``off`` yields none; ``auto`` yields the measured-safe list restricted to
    what FFmpeg actually provides; an explicit name yields just that name.
    Pure apart from the availability query, so it is straightforward to test.
    """
    if not mode or mode == HW_OFF:
        return ()
    if mode == HW_AUTO:
        available = set(available_hw_devices())
        return tuple(d for d in HW_AUTO_ORDER if d in available)
    return (mode,)


def codec_name_for(codec: CodecId) -> str:
    try:
        return _CODEC_NAMES[codec]
    except KeyError as exc:
        raise ValueError(f"unsupported video codec {codec!r}") from exc


@dataclass
class DecodedFrame:
    """A decoded image as three owned, tightly-packed YUV planes.

    Each plane is a standalone ``uint8`` array with no row padding, so it can be
    handed to ``glTexImage2D`` (or a QImage) without stride juggling.

    Planes are extracted from ``frame.planes`` and **copied**. That copy is not
    optional: FFmpeg buffers are padded (a 320px-wide plane has a 384px line
    size) and their memory is owned by the ``av.VideoFrame``, so a plain numpy
    view would both mis-stride and dangle once the frame is collected.
    """

    y: np.ndarray
    u: np.ndarray
    v: np.ndarray
    width: int
    height: int

    #: ``perf_counter_ns()`` stamped when this frame reached the decoder,
    #: before the packet was handed to FFmpeg.
    recv_ns: int = 0
    #: ``perf_counter_ns()`` once decoding finished.
    decoded_ns: int = 0
    #: Device presentation timestamp in microseconds (its own monotonic clock).
    pts_us: int = 0
    #: Wall-clock time spent inside the decoder for this frame.
    decode_ms: float = 0.0

    @property
    def chroma_width(self) -> int:
        return int(self.u.shape[1])

    @property
    def chroma_height(self) -> int:
        return int(self.u.shape[0])

    @property
    def planes(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return self.y, self.u, self.v

    def packed(self) -> np.ndarray:
        """Contiguous ``(height * 3 // 2, width)`` planar buffer.

        Chroma is placed left/right in the trailing rows, matching FFmpeg's
        convention.

        Not used in the render path -- the GPU path uploads the three planes
        as three textures and the CPU fallback (``color.yuv420_to_rgb``) reads
        them in place, so nothing needs a packed buffer. Kept for tests and
        for anything that wants a single contiguous image.
        """
        ch, cw = self.chroma_height, self.chroma_width
        out = np.empty((self.height + ch, self.width), dtype=np.uint8)
        out[: self.height, :] = self.y
        out[self.height : self.height + ch, :cw] = self.u
        out[self.height : self.height + ch, cw : cw * 2] = self.v
        return out


class VideoDecoder:
    """Wraps an FFmpeg decoder context behind a reset-friendly interface.

    ``hw_decode`` accepts ``off`` (default), ``auto``, or an explicit FFmpeg
    hardware device type. Hardware is never assumed to be faster: if the
    backend cannot be *opened*, the next candidate is tried and software is
    the last resort.

    Note the limit of that guarantee -- it covers opening only. A backend that
    opens and then fails at runtime (a broken GPU decode path, say) does *not*
    fall back to software: those frames are counted in
    :attr:`conversion_failures` and simply dropped. Add a runtime retry here
    if that ever matters; the counter exists so the failure is visible first.
    """

    def __init__(
        self,
        codec: CodecId = CodecId.H264,
        thread_count: int = DEFAULT_THREAD_COUNT,
        hw_decode: str | None = HW_OFF,
        thread_type: str = DEFAULT_THREAD_TYPE,
    ) -> None:
        self._name = codec_name_for(codec)
        self._thread_count = thread_count
        self._thread_type = thread_type
        self._hw_mode = hw_decode or HW_OFF
        self._ctx: av.CodecContext | None = None
        self.backend = SOFTWARE_BACKEND
        # RLock, not Lock: feed() holds this while counting consecutive decode
        # errors, and past the threshold it calls reset() (see feed), which
        # takes the lock again from this same thread. A plain Lock is
        # non-reentrant, so 61 consecutive FFmpegErrors -- exactly the corrupt
        # stream this recovery exists for -- would wedge the reader thread
        # forever, holding the lock, with no error ever surfaced.
        self._lock = threading.RLock()
        self._consecutive_errors = 0
        self._open()

    def _create_context(self, device_type: str | None) -> av.CodecContext:
        if device_type:
            ctx = av.CodecContext.create(
                self._name, "r", hwaccel=_hwaccel.HWAccel(device_type=device_type)
            )
        else:
            ctx = av.CodecContext.create(self._name, "r")
        try:
            # Hardware contexts keep SLICE: their latency is dominated by the
            # GPU->CPU copy and FRAME+hw is an unmeasured combination.
            ctx.thread_type = "SLICE" if device_type else self._thread_type
        except (AttributeError, ValueError, av.FFmpegError):
            pass
        if self._thread_count and not device_type:
            try:
                ctx.thread_count = self._thread_count
            except (AttributeError, ValueError, av.FFmpegError):
                pass
        return ctx

    def _open(self) -> None:
        # Try each requested accelerator, then fall back to software. Any
        # failure here is survivable -- decoding still has to work.
        for device_type in resolve_hw_types(self._hw_mode):
            try:
                self._ctx = self._create_context(device_type)
                self.backend = device_type
                return
            except Exception:
                continue
        self._ctx = self._create_context(None)
        self.backend = SOFTWARE_BACKEND

    @property
    def uses_hardware(self) -> bool:
        return self.backend != SOFTWARE_BACKEND

    @property
    def consecutive_errors(self) -> int:
        return self._consecutive_errors

    @staticmethod
    def _retire(ctx: av.CodecContext | None) -> None:
        """Reset a context's workers just before it is freed.

        Frame threading leaves worker threads parked waiting for the next
        submit; freeing the context joins them first, so a worker stuck
        behind a corrupt/never-finished frame deadlocks the join (reproduced:
        garbage packet + FRAME/2 + close hangs forever). ``flush_buffers``
        hands every worker a clean reset, and the same join then returns
        immediately -- also verified with the worker parked.
        """
        if ctx is None:
            return
        try:
            ctx.flush_buffers()
        except (AttributeError, ValueError, av.FFmpegError):
            pass

    def reset(self) -> None:
        """Drop decoder state -- used when the encoder session restarts."""
        with self._lock:
            self._retire(self._ctx)
            self._ctx = None
            self._open()

    def feed(self, packet: MediaPacket) -> list[DecodedFrame]:
        """Decode one wire packet. Never raises on bad input; returns [].

        Timing is stamped here for the latency HUD: ``recv_ns`` when the
        packet's bytes were available (before decoding), ``decoded_ns`` /
        ``decode_ms`` once the decoder is done, and the device's own ``pts``
        straight from the wire header.
        """
        out: list[DecodedFrame] = []
        recv_ns = time.perf_counter_ns()
        with self._lock:
            ctx = self._ctx
            if ctx is None or not packet.data:
                return out
            t0 = time.perf_counter_ns()
            try:
                frames = ctx.decode(av.Packet(packet.data))
            except av.FFmpegError:
                # A dropped/corrupt packet is survivable; the next keyframe
                # resyncs the stream.
                self._consecutive_errors += 1
                if self._consecutive_errors > MAX_CONSECUTIVE_ERRORS:
                    # Safe to call from inside the lock: _lock is an RLock
                    # (see __init__), so reset() re-acquiring it is a no-op
                    # rather than a self-deadlock.
                    self.reset()
                    self._consecutive_errors = 0
                return out
            decoded_ns = time.perf_counter_ns()
            decode_ms = (decoded_ns - t0) / 1e6
            self._consecutive_errors = 0
            for frame in frames:
                converted = self._convert(
                    frame,
                    recv_ns=recv_ns,
                    decoded_ns=decoded_ns,
                    pts_us=packet.pts,
                    decode_ms=decode_ms,
                )
                if converted is not None:
                    out.append(converted)
        return out

    def flush(self) -> list[DecodedFrame]:
        """Drain any frames the decoder was holding back.

        Frame threading parks up to ``thread_count`` finished frames in the
        pipeline; they only come out on a drain (or more input). The HUD
        timestamps are stamped here too so a flushed frame never renders as
        "age 0" or breaks the arrival ordering.
        """
        out: list[DecodedFrame] = []
        with self._lock:
            if self._ctx is None:
                return out
            recv_ns = time.perf_counter_ns()
            try:
                frames = self._ctx.decode(None)
            except (av.FFmpegError, ValueError):
                return out
            for frame in frames:
                converted = self._convert(
                    frame,
                    recv_ns=recv_ns,
                    decoded_ns=recv_ns,
                    pts_us=0,  # drained frames have no wire header of their own
                    decode_ms=0.0,
                )
                if converted is not None:
                    out.append(converted)
        return out

    @staticmethod
    def _fail() -> None:
        """Record a frame that could not be turned into planes.

        Every ``return None`` below used to be silent, which turned a broken
        accelerator (or an unexpected plane layout) into a black screen with
        no status message, no counter and nothing in crash.log -- the mirror
        simply never showed anything again. Counting makes that state visible
        to the HUD and to the tests.
        """
        VideoDecoder.conversion_failures += 1

    #: Frames the decoder produced but could not convert to planes. Class-level
    #: so it survives the decoder being closed and replaced.
    conversion_failures: int = 0

    @staticmethod
    def _convert(
        frame: av.VideoFrame,
        *,
        recv_ns: int = 0,
        decoded_ns: int = 0,
        pts_us: int = 0,
        decode_ms: float = 0.0,
    ) -> DecodedFrame | None:
        """Extract Y/U/V as owned, unpadded arrays.

        We deliberately do not use ``to_ndarray(format='yuv420p')``: its packing
        of padded planes is undocumented, whereas ``frame.planes`` gives
        ``line_size`` explicitly and is unambiguous.
        """
        if frame.width <= 0 or frame.height <= 0:
            VideoDecoder._fail()
            return None
        try:
            ref = frame.reformat(format="yuv420p")
        except (av.FFmpegError, ValueError) as exc:
            VideoDecoder._fail()
            if VideoDecoder.conversion_failures % 60 == 1:
                print(f"[droidmirror] frame conversion failed: {exc}")
            return None

        planes: list[np.ndarray] = []
        try:
            for plane in ref.planes:
                line, h, w = plane.line_size, plane.height, plane.width
                if line < w or h <= 0 or w <= 0:
                    VideoDecoder._fail()
                    return None
                buf = np.frombuffer(plane, dtype=np.uint8, count=line * h)
                # .copy() detaches from the frame-owned buffer and strips padding.
                planes.append(buf.reshape(h, line)[:, :w].copy())
        except (TypeError, ValueError, av.FFmpegError):
            VideoDecoder._fail()
            return None

        if len(planes) < 3:
            VideoDecoder._fail()
            return None
        y, u, v = planes[0], planes[1], planes[2]
        return DecodedFrame(
            y=y,
            u=u,
            v=v,
            width=frame.width,
            height=frame.height,
            recv_ns=recv_ns,
            decoded_ns=decoded_ns,
            pts_us=pts_us,
            decode_ms=decode_ms,
        )

    def close(self) -> None:
        with self._lock:
            self._retire(self._ctx)
            self._ctx = None

    def __del__(self) -> None:
        # Test code and hot paths often drop a decoder without closing it;
        # GC frees the codec context directly, which would join parked frame
        # workers without the flush and deadlock (reproduced with
        # `del dec; gc.collect()`). Retire through close() instead.
        try:
            self.close()
        except Exception:
            pass  # interpreter shutdown: modules may already be torn down
