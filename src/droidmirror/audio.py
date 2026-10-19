"""Device audio path: scrcpy audio socket -> opus decode -> speakers.

The scrcpy server (v4.1) streams capture on a dedicated socket, dialed
between video and control. Its first 4 bytes are the codec id; every packet
after that reuses the video socket's 12-byte frame header (pts in the low 61
bits, config flag at bit 62).

Playback mirrors what the official client does (app/src/audio_regulator.c):
decode arriving packets roughly as fast as they land (the encoder paces them
in real time), keep a short jitter buffer, pad silence on underrun, and never
stall the socket.

Note the backlog handling is capacity-based, not pts-based: ``PcmBuffer`` drops
its oldest chunks once ``MAX_BUFFER_S`` is exceeded. There is no pts clock
comparison here (unlike the video path's LagMeter), so if decoding ever falls
behind realtime the output desyncs rather than skipping to live.

Threading: one reader thread (recv -> demux -> decode -> buffer) and the
PortAudio callback thread pulling from the buffer. Both are daemons; closing
the socket from ``stop()`` is what wakes the reader.
"""

from __future__ import annotations

import socket
import threading
from typing import Callable

import av
import numpy as np

from .protocol import (
    AudioCodecId,
    AudioDemuxer,
    ProtocolError,
)

#: scrcpy's audio default (server/.../AudioConfig.java): 48 kHz stereo PCM.
SAMPLE_RATE = 48_000
CHANNELS = 2

#: Jitter/catch-up policy: how much decoded PCM we are willing to hold.
MAX_BUFFER_S = 0.5

#: How many opus packets may fail to decode back to back before audio is
#: abandoned for this session. Opus resynchronises on the next packet, so a
#: lone corrupt packet is survivable; a run of them means the stream is gone.
MAX_CONSECUTIVE_DECODE_ERRORS = 10

#: Decoder construction failures are reported through this callback.
StatusCallback = Callable[[str, str], None]

#: Sink contract: called with interleaved int16 bytes; ``None`` stops it.
Sink = Callable[[bytes | None], None]


class PcmBuffer:
    """Thread-safe FIFO of decoded PCM with silence padding on underrun.

    The reader thread writes, the audio callback reads. Bounded: when the
    producer outruns playback (pts jumps, catch-up), the *oldest* samples are
    dropped so playback resyncs to now instead of lagging forever.
    """

    def __init__(self, max_seconds: float = MAX_BUFFER_S) -> None:
        self._max_bytes = int(max_seconds * SAMPLE_RATE) * 2 * CHANNELS
        self._chunks: list[bytes] = []
        self._buffered = 0
        self._lock = threading.Lock()

    @property
    def buffered_bytes(self) -> int:
        with self._lock:
            return self._buffered

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._buffered = 0

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            self._chunks.append(pcm)
            self._buffered += len(pcm)
            while self._buffered > self._max_bytes and len(self._chunks) > 1:
                dropped = self._chunks.pop(0)
                self._buffered -= len(dropped)

    def read(self, byte_count: int) -> bytes:
        """Pop up to ``byte_count`` bytes; silence-pad the remainder."""
        with self._lock:
            out = bytearray()
            while self._chunks and len(out) < byte_count:
                head = self._chunks[0]
                need = byte_count - len(out)
                if len(head) <= need:
                    out.extend(head)
                    self._chunks.pop(0)
                    self._buffered -= len(head)
                else:
                    out.extend(head[:need])
                    self._chunks[0] = head[need:]
                    self._buffered -= need
        if len(out) < byte_count:
            out.extend(b"\x00" * (byte_count - len(out)))
        return bytes(out)


def make_decoder() -> object:
    """Build the opus decoder the way the official FFmpeg client does:
    low delay, 48 kHz, stereo, no extradata (config packets are dropped)."""
    ctx = av.codec.CodecContext.create("opus", "r")
    ctx.flags |= av.codec.context.Flags.low_delay
    ctx.sample_rate = SAMPLE_RATE
    ctx.layout = "stereo"
    ctx.open()
    return ctx


def decode_packet(ctx, payload: bytes) -> bytes:
    """One opus packet -> interleaved int16 PCM bytes.

    libopus decodes to planar float in [-1, 1]; that must be scaled to the
    int16 range, not truncated (which would render silence).
    """
    frames = ctx.decode(av.Packet(payload))
    if not frames:
        return b""
    chunks = []
    for frame in frames:
        arr = frame.to_ndarray()
        if arr.ndim == 2 and arr.shape[0] <= 8 and arr.shape[1] > 8:
            arr = arr.T  # planar (channels, samples) -> (samples, channels)
        if arr.dtype in (np.float32, np.float64):
            arr = np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)
        elif arr.dtype != np.int16:
            arr = arr.astype(np.int16, copy=False)
        chunks.append(np.ascontiguousarray(arr).tobytes())
    return b"".join(chunks)


def make_sink(buffer: PcmBuffer) -> Sink:
    """Lazily start a PortAudio stream that pulls PCM from ``buffer``."""
    import sounddevice as sd

    state: dict = {}

    def callback(outdata, frames, _time, _status) -> None:
        pcm = buffer.read(frames * CHANNELS * 2)
        outdata[:] = np.frombuffer(pcm, dtype=np.int16).reshape(-1, CHANNELS)

    def sink(data: bytes | None) -> None:
        if data is None:
            # Shutdown: only close a stream that actually got created.
            stream = state.pop("stream", None)
            if stream is not None:
                stream.stop()
                stream.close()
            return
        buffer.write(data)
        if state.get("stream") is None:
            stream = sd.OutputStream(
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                callback=callback,
            )
            stream.start()
            state["stream"] = stream

    return sink


class AudioStream(threading.Thread):
    """Reads one scrcpy audio socket until it closes or ``stop()`` is called.

    ``sink`` and ``decoder_factory`` are injectable for tests; the defaults
    open the real opus decoder and the real audio device.
    """

    def __init__(
        self,
        sock: socket.socket,
        on_status: StatusCallback | None = None,
        *,
        sink: Sink | None = None,
        decoder_factory: Callable[[], object] | None = None,
        mute: bool = False,
    ) -> None:
        super().__init__(name="droidmirror-audio", daemon=True)
        self._sock = sock
        self._on_status = on_status
        self._buffer = PcmBuffer()
        self._sink = sink or make_sink(self._buffer)
        self._decoder_factory = decoder_factory or make_decoder
        self._stop = threading.Event()
        self._mute = threading.Event()
        if mute:
            self._mute.set()
        self._decoder = None
        self.packets_decoded = 0
        self.codec: AudioCodecId | None = None

    @property
    def muted(self) -> bool:
        return self._mute.is_set()

    def set_muted(self, muted: bool) -> None:
        """Silence (or restore) playback without touching the socket.

        The buffer is dropped on mute: it holds up to ``MAX_BUFFER_S`` of
        already-decoded sound, so without this un-muting would replay a stale
        half-second before catching up. While muted the packets are dropped
        before decoding, so a muted stream costs almost nothing.
        """
        if muted:
            self._mute.set()
            self._buffer.clear()
        else:
            self._mute.clear()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _status(self, message: str, level: str = "info") -> None:
        if self._on_status:
            self._on_status(message, level)

    def run(self) -> None:
        demuxer = AudioDemuxer()
        started = False
        errors = 0
        abort = False
        try:
            while not self._stop.is_set() and not abort:
                try:
                    chunk = self._sock.recv(1 << 16)
                except OSError:  # socket.timeout is an OSError since 3.10
                    break
                if not chunk:
                    break
                try:
                    events = demuxer.feed(chunk)
                except ProtocolError as exc:
                    self._status(f"Audio protocol error: {exc}", "error")
                    break

                if self.codec is None and demuxer.codec is not None:
                    self.codec = demuxer.codec
                    if not self._handle_codec(demuxer):
                        break

                for packet in events:
                    if packet.config:
                        continue  # OpusHead arrives as a config packet; dropped
                    if self._mute.is_set():
                        continue  # muted: don't even pay for decoding
                    if self._decoder is None:
                        continue
                    try:
                        pcm = decode_packet(self._decoder, packet.data)
                    except Exception as exc:
                        # Decoder blowups must not kill video -- but a single
                        # corrupt opus packet must not kill audio either. Opus
                        # resynchronises on the next packet, so drop this one,
                        # count it, and only give up once the stream is clearly
                        # broken rather than damaged. Previously any failure
                        # here ended audio permanently for the session, with no
                        # way back short of reconnecting.
                        errors += 1
                        if errors <= MAX_CONSECUTIVE_DECODE_ERRORS:
                            if errors == 1:
                                self._status(
                                    f"Audio decode error (skipping packet): {exc}"
                                )
                            continue
                        self._status(
                            f"Audio decode failed {errors} times in a row - "
                            f"continuing without sound: {exc}",
                            "error",
                        )
                        abort = True
                        break
                    if pcm:
                        errors = 0  # a good packet clears the strike count
                        if not started:
                            started = True
                            self._status("Audio: playing on this computer")
                        self._sink(pcm)
                        self.packets_decoded += 1
        finally:
            self._sink(None)  # close the playback stream if we opened one

    def _handle_codec(self, demuxer: AudioDemuxer) -> bool:
        """Set up decoding for the negotiated codec. False = stop this stream."""
        codec = demuxer.codec
        if codec == AudioCodecId.DISABLED:
            self._status("Device audio unavailable - continuing without sound.")
            return False
        if codec == AudioCodecId.ERROR:
            self._status("Device audio config error.", "error")
            return False
        if codec != AudioCodecId.OPUS:
            name = codec.name if codec is not None else "?"
            self._status(
                f"Audio codec {name} is not supported - no sound.", "error"
            )
            return False
        try:
            self._decoder = self._decoder_factory()
        except Exception as exc:
            self._status(f"Could not start the audio decoder: {exc}", "error")
            return False
        return True
