"""When the track hits -- the beat map the promo is cut on.

Decodes crush.mp3 through the ffmpeg kept in temp (nothing installed), finds
the onsets, and builds a cut list out of them.

Three detectors were tried on this track before this one, and the failures are
worth keeping because none of them look wrong in the numbers:

* broadband spectral flux, global threshold: on a mix that is loud throughout,
  a global threshold finds nothing in the quiet bars and everything in the
  loud ones. The onsets have to be judged against the *local* level.
* low band only (30-200 Hz): the kick is not isolated on this track, the low
  end is a continuous wash, so its peaks are noise.
* derivatives of the low band: near-silent between hits, which makes the
  median 0 and any median-based threshold collapse to "everything is an
  onset".

What works is the standard one: log-compressed broadband spectral change,
divided by its own 2-second moving average so it is measured against the
music around it, then peaks. Cuts are then taken from the onsets themselves
(spaced at least two beats apart), so a cut lands on a real hit even if the
tempo estimate is a hair off -- and the tempo is only used for the spacing.

Writes promo/beats.json. A build tool for the promo, not part of the app.

    .venv\\Scripts\\python.exe promo\\beats.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

FFMPEG = Path(
    r"C:\Users\Hardik\AppData\Local\Temp\opencode\viddeps\imageio_ffmpeg\binaries"
    r"\ffmpeg-win-x86_64-v7.1.exe"
)
TRACK = Path(
    r"C:\Users\Hardik\Downloads\Projects\music-flow-0.1.0\music-flow-0.1.0\crush.mp3"
)
OUT = Path(__file__).resolve().parent / "beats.json"

SR = 22050
WIN = 1024
HOP = 256
ENV_HZ = SR / HOP  # frames per second, ~86.13
PEAK_RATIO = 1.6  # how far above the local level a hit has to stand
MIN_GAP = 0.12  # seconds; the fastest two hits can be
SNAP = 0.08  # a cut may move this far to sit on a hit
LEAD = 40.0  # seconds of music before the loudest moment
TAIL = 5.0  # seconds after it, for the end card


def decode() -> np.ndarray:
    raw = subprocess.run(
        [
            str(FFMPEG), "-v", "error", "-i", str(TRACK),
            "-f", "f32le", "-ac", "1", "-ar", str(SR), "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return np.frombuffer(raw, dtype="<f4").astype(np.float64)


def smooth(signal: np.ndarray, seconds: float) -> np.ndarray:
    span = max(1, int(seconds * ENV_HZ))
    return np.convolve(signal, np.ones(span) / span, mode="same")


def analysis(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(onset novelty, broadband level) per frame."""
    count = 1 + (len(x) - WIN) // HOP
    starts = HOP * np.arange(count)[:, None]
    windowed = x[starts + np.arange(WIN)[None, :]] * np.hanning(WIN)

    spect = np.abs(np.fft.rfft(windowed, axis=1))
    log = np.log1p(spect)
    delta = np.diff(log, axis=0)
    delta[delta < 0.0] = 0.0
    novelty = smooth(delta.sum(axis=1), 0.02)
    level = np.sqrt((windowed**2).mean(axis=1))
    return novelty, level


def onsets(novelty: np.ndarray, ratio: float = PEAK_RATIO) -> tuple[np.ndarray, np.ndarray]:
    """Frames where a hit starts, judged against the local level."""
    local = smooth(novelty, 2.0)
    relative = novelty / (local + 1e-12)
    gap = int(MIN_GAP * ENV_HZ)
    peaks = [
        i
        for i in range(1, len(relative) - 1)
        if relative[i] >= ratio
        and relative[i] > relative[i - 1]
        and relative[i] >= relative[i + 1]
    ]
    kept: list[int] = []
    for i in sorted(peaks, key=lambda i: relative[i], reverse=True):
        if all(abs(i - j) >= gap for j in kept):
            kept.append(i)
    return np.array(sorted(kept)), relative


def tempo_from_onsets(times: np.ndarray) -> float:
    """The period the hits repeat at, from the spacing histogram."""
    gaps = np.diff(times)
    gaps = gaps[(gaps > 0.20) & (gaps < 1.40)]
    if len(gaps) < 8:
        return 0.4
    histogram, edges = np.histogram(gaps, bins=np.arange(0.20, 1.40, 0.01))
    best = int(histogram.argmax())
    centre = (edges[best] + edges[best + 1]) / 2
    near = gaps[np.abs(gaps - centre) < 0.05]
    return float(near.mean())


def grid_fit(times: np.ndarray, period: float) -> tuple[float, float]:
    """(phase, coverage): the offset whose grid sits on the most onsets.

    Coverage is the share of onsets within 40 ms of a grid line; guessing
    scores ``0.08 / period``.
    """
    best_phase, best_cover = 0.0, -1.0
    for phase in np.arange(0.0, period, 1.0 / ENV_HZ):
        offsets = (times - phase) % period
        hits = np.minimum(offsets, period - offsets) <= 0.04
        if hits.mean() > best_cover:
            best_phase, best_cover = float(phase), float(hits.mean())
    return best_phase, best_cover


def cut_points(times: np.ndarray, period: float, beats: float = 2.0) -> list[float]:
    """Onsets at least ``beats`` apart -- every cut is a real hit."""
    if len(times) == 0:
        return []
    minimum = period * beats * 0.85  # a little swing is still a beat
    cuts = [float(times[0])]
    for t in times[1:]:
        if t - cuts[-1] >= minimum:
            cuts.append(float(t))
    return cuts


def main() -> None:
    samples = decode()
    duration = len(samples) / SR
    novelty, level = analysis(samples)
    onset_frames, relative = onsets(novelty)
    onset_times = (onset_frames * HOP + WIN / 2) / SR

    period = tempo_from_onsets(onset_times)
    phase, coverage = grid_fit(onset_times, period) if len(onset_times) else (0.0, 0.0)
    guess = 0.08 / period

    secs = int(ENV_HZ)
    # Anchor on the loudest moment, then take the *later* of any that tie: the
    # last climax is the one a promo should build to (the track lifts twice,
    # equally loud, and the first lift would leave the video peaking in the
    # middle with nowhere to go).
    per_second = smooth(level, 3.0)[::secs]
    loudest_candidates = np.flatnonzero(per_second >= per_second.max() * 0.98)
    loudest = int(loudest_candidates[-1])
    start = max(0.0, loudest - LEAD)
    end = min(duration, loudest + TAIL)
    section = (round(start, 2), round(end, 2))

    cuts_in_section = [c for c in cut_points(onset_times, period) if start <= c <= end]
    rising = smooth(level, 0.4)
    rise = np.diff(rising)
    rises: list[float] = []
    for i in np.argsort(rise)[::-1]:
        if rise[i] <= 0 or not 8 < i < len(rise) - 1:
            continue
        t = (i * HOP + WIN / 2) / SR
        if all(abs(t - r) > 2.5 for r in rises):
            rises.append(round(float(t), 3))
        if len(rises) == 5:
            break
    rises.sort()

    beats = np.arange(phase - period * np.ceil(phase / period), duration, period)

    OUT.write_text(
        json.dumps(
            {
                "track": str(TRACK),
                "duration": round(duration, 3),
                "bpm": round(60.0 / period, 3),
                "beat_period": round(period, 6),
                "coverage": round(coverage, 4),
                "beats": [round(float(b), 4) for b in beats],
                "onset_count": len(onset_times),
                "onsets": [round(float(t), 4) for t in onset_times],
                "cut_candidates": [round(c, 4) for c in cut_points(onset_times, period)],
                "section": list(section),
                "section_cuts": [round(c, 4) for c in cuts_in_section],
                "rises": rises,
                "level_per_second": [round(float(level[i]), 5) for i in range(0, len(level), secs)],
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    kept_ratio = relative[onset_frames] if len(onset_frames) else np.array([0.0])
    print(f"track      {TRACK.name}  {duration:.2f}s")
    print(f"onsets     {len(onset_times)}  ({len(onset_times) / duration:.2f}/s), "
          f"peak ratio median {np.median(kept_ratio):.2f}, max {kept_ratio.max():.2f}")
    print(f"tempo      {60.0 / period:.2f} bpm  ({period * 1000:.1f} ms/beat)")
    print(f"lock       {coverage * 100:.1f}% of onsets on the grid "
          f"(guessing {guess * 100:.1f}%)")
    print(f"section    {section[0]:.2f}s -> {section[1]:.2f}s, "
          f"loudest at {loudest:.1f}s, {len(cuts_in_section)} cuts")
    print(f"rises      {rises}")
    print()
    print("  onset check: '#' novelty, 'K' a kept onset -- quiet bar then loud")
    for label, from_s in (("quiet 124.8s", 124.8), ("loud 138.9s", 138.9)):
        print(f"  -- {label}")
        first = int(from_s * ENV_HZ)
        step = max(1, int(0.04 * ENV_HZ))
        peak = novelty.max()
        for i in range(first, first + int(2 * ENV_HZ), step):
            marks = "".join(
                "K" if j in set(onset_frames.tolist()) else " " for j in range(i, i + step)
            )
            print(
                f"    {i / ENV_HZ:7.2f}s {marks:<{step}} "
                f"{'#' * int(novelty[i : i + step].max() / peak * 24)}"
            )
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
