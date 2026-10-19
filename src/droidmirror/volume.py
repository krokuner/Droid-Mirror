"""Tablet media-volume control over adb.

scrcpy's control protocol has no "set volume" message. ``INJECT_KEYCODE``
could raise or lower the volume one step at a time (``KEYCODE_VOLUME_UP`` /
``VOLUME_DOWN``, both already defined in :mod:`droidmirror.protocol`), but
nothing reads the level back and nothing can jump straight to a value -- so a
slider cannot be built on it. This goes over adb instead, the same way the
keyguard and foreground detectors do.

Both commands were verified against a real SM-X210 on Android 16 (SDK 36)::

    read   dumpsys audio                                  ~270 ms, ~1500 lines
    write  cmd media_session volume --stream 3 --set N

Two of the obvious alternatives do *not* work on that build, and are recorded
here so they are not tried again:

* ``media volume --stream 3 --set N`` -- ``/system/bin/media`` no longer
  exists on Android 16 (``sh: media: inaccessible or not found``). It is the
  command every older guide names, which is exactly why it is worth writing
  down that it is gone.
* ``cmd media_session volume --stream 3 --show`` -- prints only its own
  verbose log (``[V] will control stream=3`` ...) and never the index, with or
  without ``--stream``, and in either flag order.

A read therefore costs a quarter of a second and a lot of output, which is why
the UI asks once on connect rather than on a timer.

Parsing is a pure function over the dump text so it can be tested against
captured samples with no device attached -- the same shape as
:mod:`droidmirror.lockscreen`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .adb import Adb

#: AudioManager.STREAM_MUSIC -- the stream `cmd media_session volume` takes.
MEDIA_STREAM = 3

#: The block name `dumpsys audio` prints it under.
STREAM_NAME = "STREAM_MUSIC"

#: How long a volume command may take before it is abandoned. This runs off the
#: GUI thread, but a wedged adb should still not pin a worker forever.
COMMAND_TIMEOUT_S = 8.0


@dataclass(frozen=True)
class VolumeState:
    """The tablet's media volume, as the device reports it."""

    index: int
    maximum: int
    muted: bool = False

    @property
    def percent(self) -> float:
        """``0.0``..``1.0``, or ``0.0`` if the device reports no range."""
        if self.maximum <= 0:
            return 0.0
        return min(1.0, max(0.0, self.index / self.maximum))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

#: The stream's own block header. Anchored to the whole line and to the exact
#: name, because `dumpsys audio` prints two other blocks that also contain
#: "STREAM_MUSIC" -- ``- STREAM_TTS (aliased to: STREAM_MUSIC):`` and
#: ``- STREAM_ACCESSIBILITY (aliased to: STREAM_MUSIC):``. A loose search would
#: happily report the accessibility stream's volume as the music volume, and
#: the two only happen to agree.
_BLOCK_START = re.compile(r"^\s*-\s*" + re.escape(STREAM_NAME) + r":\s*$", re.M)

#: Any following block header ends this one.
_BLOCK_END = re.compile(r"^\s*-\s+\S", re.M)

#: The absolute index, as printed by modern Android (this is the one field
#: that is unambiguous -- see parse_stream_volume).
_INDEX_RE = re.compile(r"^\s*streamVolume:\s*(\d+)\s*$", re.M)

_MAX_RE = re.compile(r"^\s*Max:\s*(\d+)\s*$", re.M)

#: ``Muted:`` exactly. `Muted Internally: false` sits directly beneath it and
#: must not be mistaken for it, hence the ``:`` immediately after the word.
_MUTED_RE = re.compile(r"^\s*Muted:\s*(true|false)\s*$", re.M)


def parse_stream_volume(
    dump: str, stream: str = STREAM_NAME
) -> VolumeState | None:
    """Pull one stream's volume out of a ``dumpsys audio`` dump.

    Returns ``None`` when the dump does not state it -- an unrecognised build,
    a truncated read, or a device that prints a different shape.

    Deliberately reads only the ``streamVolume:`` field. Older Android prints a
    bare ``Current: 5 (speaker): 5\\50`` line instead, but *newer* Android also
    prints ``Current: 1 (earpiece): 5\\50, 2 (speaker): 5\\50, ...`` -- where
    that first number is a device id, not a volume. The two are
    indistinguishable without counting the rest of the list, so guessing would
    mean reading 1 as the volume on every modern device. Without
    ``streamVolume:`` this returns ``None`` and the UI disables itself, which
    is the honest outcome.
    """
    if not dump:
        return None

    header = re.compile(r"^\s*-\s*" + re.escape(stream) + r":\s*$", re.M)
    match = header.search(dump)
    if not match:
        return None

    rest = dump[match.end() :]
    end = _BLOCK_END.search(rest)
    block = rest[: end.start()] if end else rest

    index = _INDEX_RE.search(block)
    maximum = _MAX_RE.search(block)
    if index is None or maximum is None:
        return None

    muted = _MUTED_RE.search(block)
    return VolumeState(
        index=int(index.group(1)),
        maximum=int(maximum.group(1)),
        muted=bool(muted) and muted.group(1) == "true",
    )


# --------------------------------------------------------------------------- #
# Device access
# --------------------------------------------------------------------------- #


def get_volume(adb: "Adb", stream: str = STREAM_NAME) -> VolumeState | None:
    """Ask the tablet for its media volume.

    Blocking (~270 ms on a real device), so call it off the GUI thread. Never
    raises: a failed read is ``None``, meaning "unknown", never zero -- a
    transient adb hiccup must not present itself to the user as a stream that
    has been turned down.
    """
    try:
        result = adb.shell("dumpsys audio", check=False, timeout=COMMAND_TIMEOUT_S)
    except Exception:
        return None
    return parse_stream_volume(getattr(result, "stdout", "") or "", stream)


def _run(adb: "Adb", command: str) -> bool:
    try:
        proc = adb.shell(command, check=False, timeout=COMMAND_TIMEOUT_S)
    except Exception:
        return False
    # adb reports most failures on stdout with a zero exit status, so the text
    # is what has to be inspected -- the same lesson as Adb.connect().
    text = ((getattr(proc, "stdout", "") or "") + (getattr(proc, "stderr", "") or ""))
    lowered = text.lower()
    return "not found" not in lowered and "inaccessible" not in lowered


def set_volume(adb: "Adb", index: int, stream: int = MEDIA_STREAM) -> bool:
    """Set the tablet's media volume to an absolute index. Blocking.

    The device clamps to its own range, so the index is only guarded against
    being negative here; the caller is expected to have taken the maximum from
    :func:`get_volume`.

    ``cmd media_session`` is the modern spelling and needs Android 11+. Before
    that the equivalent was ``media volume``; both are attempted so an older
    tablet can still be controlled. Only the first is exercised on the device
    this was written against (Android 16, where ``media`` no longer exists).
    """
    index = max(0, int(index))
    if _run(adb, f"cmd media_session volume --stream {int(stream)} --set {index}"):
        return True
    return _run(adb, f"media volume --stream {int(stream)} --set {index}")
