"""Tablet volume: dumpsys parsing, the adb commands, and the fallback.

The dumps below are trimmed transcripts of real output from an SM-X210 on
Android 16 (SDK 36) -- including the two things that make this parse fiddly:
the `Current:` line wraps at column 0, and two *other* blocks in the same dump
have "STREAM_MUSIC" in their header.
"""

from __future__ import annotations

import pytest

from droidmirror.session import MirrorSession
from droidmirror.volume import (
    MEDIA_STREAM,
    VolumeState,
    get_volume,
    parse_stream_volume,
    set_volume,
)

#: Real shape, shortened. Note `80 (bt_a2dp)` starts at column 0 (the device
#: wraps that line), and STREAM_TTS / STREAM_ACCESSIBILITY both carry
#: "STREAM_MUSIC" in their headers with *different* volumes -- an unanchored
#: search for "STREAM_MUSIC" finds the accessibility stream instead.
REAL_DUMP = """
Audio Manager state:
  AudioEventLogger History:
  Stream volumes (device: 3)
  ---------------------------
  - STREAM_MUSIC:
     Muted: false
     Muted Internally: false
     Min: 0
     Max: 15
     streamVolume:5
     Current: 1 (earpiece): 5\\50, 2 (speaker): 5\\50, 4 (headset): 9\\90,
80 (bt_a2dp): 5\\49, 8000 (remote_submix): 8\\80, 40000000 (default): 7\\70
   Volume Group: AUDIO_STREAM_MUSIC

  - STREAM_ALARM:
     Muted: false
     Muted Internally: false
     Min: 1
     Max: 15
     streamVolume:7

  - STREAM_ACCESSIBILITY (aliased to: STREAM_MUSIC):
     Muted: false
     Muted Internally: false
     Min: 1
     Max: 15
     streamVolume:6

  - STREAM_TTS (aliased to: STREAM_MUSIC):
     Muted: false
     Muted Internally: false
     Min: 0
     Max: 15
     streamVolume:5
"""

GARBAGE = """
nothing about audio at all, just a scheduler dump and some timestamps
"""


class _Result:
    def __init__(self, stdout: str = "", stderr: str = "") -> None:
        self.stdout = stdout
        self.stderr = stderr


class FakeAdb:
    """Serves canned shell answers and records the commands it was given."""

    def __init__(self, answer=None, *, raises: bool = False) -> None:
        self._answer = answer
        self._raises = raises
        self.commands: list[str] = []

    def shell(self, command, **_kwargs):
        self.commands.append(command)
        if self._raises:
            raise OSError("adb went away")
        if callable(self._answer):
            return self._answer(command)
        return self._answer if self._answer is not None else _Result()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_the_real_dump_reads_the_music_volume():
    state = parse_stream_volume(REAL_DUMP)
    assert state == VolumeState(index=5, maximum=15, muted=False)


def test_the_aliased_blocks_are_not_mistaken_for_the_music_stream():
    """STREAM_ACCESSIBILITY is at 6 and STREAM_TTS at 5.

    Both headers contain "STREAM_MUSIC", so a loose match would report the
    accessibility stream's volume. The exact-name anchor is what stops that.
    """
    assert parse_stream_volume(REAL_DUMP).index == 5

    # And prove the sample really would catch a loose matcher: the accessibility
    # block's own header does contain the string.
    assert "- STREAM_ACCESSIBILITY (aliased to: STREAM_MUSIC):" in REAL_DUMP


def test_the_block_ends_at_the_next_stream():
    """STREAM_ALARM is at 7; the music block must not pick it up."""
    assert parse_stream_volume(REAL_DUMP).index != 7


def test_a_muted_stream_is_reported_as_muted():
    dump = REAL_DUMP.replace(
        "  - STREAM_MUSIC:\n     Muted: false",
        "  - STREAM_MUSIC:\n     Muted: true",
    )
    assert parse_stream_volume(dump).muted is True


def test_muted_internally_is_not_read_as_the_muted_flag():
    """`Muted Internally:` sits directly under `Muted:` and shares the prefix."""
    dump = REAL_DUMP.replace("Muted: false", "Muted: true", 1)
    assert parse_stream_volume(dump).muted is True

    only_internal = """
      - STREAM_MUSIC:
         Muted Internally: true
         Min: 0
         Max: 15
         streamVolume:4
    """
    assert parse_stream_volume(only_internal).muted is False


def test_the_block_parses_when_it_is_the_last_thing_in_the_dump():
    """No following `- ` header: the block runs to the end of the text."""
    tail = REAL_DUMP.split("Volume Group: AUDIO_STREAM_MUSIC")[0]
    state = parse_stream_volume(tail)
    assert state is not None and state.index == 5


def test_a_dump_without_the_stream_is_unknown():
    assert parse_stream_volume(GARBAGE) is None


def test_an_empty_dump_is_unknown():
    assert parse_stream_volume("") is None


def test_a_block_without_the_index_is_unknown():
    """Older Android prints `Current:` instead, where the first number is a
    *device id* on modern builds -- so it must not be guessed at."""
    old = """
      - STREAM_MUSIC:
         Muted: false
         Min: 0
         Max: 15
         Current: 5 (speaker): 5\\50
    """
    assert parse_stream_volume(old) is None


def test_a_block_without_the_maximum_is_unknown():
    partial = """
      - STREAM_MUSIC:
         Muted: false
         Min: 0
         streamVolume:5
    """
    assert parse_stream_volume(partial) is None


def test_percent_covers_the_range_and_survives_a_zero_maximum():
    assert VolumeState(index=0, maximum=15).percent == 0.0
    assert VolumeState(index=15, maximum=15).percent == 1.0
    assert VolumeState(index=5, maximum=0).percent == 0.0  # no division by zero


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #


def test_get_volume_parses_what_the_device_prints():
    adb = FakeAdb(_Result(REAL_DUMP))
    assert get_volume(adb) == VolumeState(index=5, maximum=15, muted=False)
    assert adb.commands == ["dumpsys audio"]


def test_get_volume_is_none_when_adb_fails():
    """An unreadable level must read as unknown, never as 0 -- showing 0 would
    claim the tablet had been turned down."""
    assert get_volume(FakeAdb(raises=True)) is None


def test_get_volume_is_none_when_the_device_says_nothing_useful():
    assert get_volume(FakeAdb(_Result(GARBAGE))) is None


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


def test_set_volume_uses_the_modern_command():
    adb = FakeAdb(_Result())
    assert set_volume(adb, 9) is True
    assert adb.commands == [
        f"cmd media_session volume --stream {MEDIA_STREAM} --set 9"
    ]


def test_set_volume_falls_back_on_a_build_without_cmd_media_session():
    """`cmd media_session` needs Android 11+; before that it was `media`."""

    def answer(command):
        if command.startswith("cmd media_session"):
            return _Result("/system/bin/sh: cmd: inaccessible or not found")
        return _Result()

    adb = FakeAdb(answer)
    assert set_volume(adb, 3) is True
    assert adb.commands == [
        "cmd media_session volume --stream 3 --set 3",
        "media volume --stream 3 --set 3",
    ]


def test_set_volume_reports_failure_when_neither_command_works():
    adb = FakeAdb(_Result("sh: media: inaccessible or not found"))
    assert set_volume(adb, 3) is False
    assert len(adb.commands) == 2  # both were attempted


def test_set_volume_never_sends_a_negative_index():
    adb = FakeAdb(_Result())
    set_volume(adb, -4)
    assert adb.commands == [
        f"cmd media_session volume --stream {MEDIA_STREAM} --set 0"
    ]


def test_set_volume_returns_false_rather_than_raising():
    assert set_volume(FakeAdb(raises=True), 5) is False


# --------------------------------------------------------------------------- #
# Session wiring
# --------------------------------------------------------------------------- #


def test_the_session_delegates_both_directions_to_the_device():
    adb = FakeAdb(_Result(REAL_DUMP))
    session = MirrorSession(adb)  # type: ignore[arg-type]

    assert session.device_volume() == VolumeState(5, 15, False)

    adb._answer = _Result()
    assert session.set_device_volume(12) is True
    assert adb.commands[-1] == f"cmd media_session volume --stream 3 --set 12"


@pytest.mark.parametrize("index,expected", [(0, "0"), (7, "7"), (15, "15")])
def test_the_index_survives_the_round_trip_to_a_command(index, expected):
    adb = FakeAdb(_Result())
    set_volume(adb, index)
    assert adb.commands[0].endswith(f"--set {expected}")
