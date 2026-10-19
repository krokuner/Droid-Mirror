"""cut.py — the editor: takes -> beat-cut promo, music "crush" by 2hollis.

Pipeline:
  1. plan()    snaps shot boundaries onto the real cut grid from beats.json
  2. shots     renders each shot to a normalised 1920x1080/30fps clip
  3. concat    joins the clips (same codec, stream copy)
  4. mux       lays the music from section start and writes DroidMirror_promo.mp4

Usage (via .venv python):
  python cut.py            render the promo
  python cut.py --plan     print the cut sheet and exit (no render)
  python cut.py --stills   extract one frame per shot to shots_preview/ and exit
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
TAKES = HERE / "takes"
SHOTS = HERE / "shots"
PREVIEW = HERE / "shots_preview"
BEATS = HERE / "beats.json"
OUT = HERE / "DroidMirror_promo.mp4"
FFMPEG = Path(
    r"C:\Users\Hardik\AppData\Local\Temp\opencode\viddeps\imageio_ffmpeg\binaries"
    r"\ffmpeg-win-x86_64-v7.1.exe"
)

W, H, FPS = 1920, 1080, 30
MIN_SHOT = 0.5  # floor between snap candidates, seconds

# --------------------------------------------------------------------------- #
# The cut sheet: (take, in-point, wanted duration, mode)
#   modes — full : whole window, padded to 16:9
#           strip: the tablet alone, centred on a blurred blow-up of itself
#           zoom : punch-in on the mirror area (hides chrome and toast)
#           inset: near-full crop, trims the toolbar edge and toast
# --------------------------------------------------------------------------- #


@dataclass
class Shot:
    take: str
    inn: float
    want: float
    mode: str = "full"
    at: float = 0.0   # snapped start in the section
    dur: float = 0.0  # snapped duration

    def src(self) -> Path:
        return TAKES / f"{self.take}.mp4"


SHOT_LIST: list[Shot] = [
    # cold open — the connect ceremony
    Shot("01_connect", 1.5, 3.8, "full"),
    Shot("01_connect", 21.0, 4.4, "full"),
    # live mirror, small hands
    Shot("02_mirror_home", 1.0, 2.2, "strip"),
    Shot("03_touch_drag", 0.6, 2.2, "strip"),
    Shot("03_touch_drag", 6.6, 2.4, "strip"),
    # keybinds — the banger
    Shot("04_keybinds_load", 0.6, 2.0, "full"),
    Shot("04_keybinds_load", 4.2, 2.2, "full"),
    Shot("05_granny_wasd", 1.0, 1.4, "strip"),
    Shot("05_granny_wasd", 2.7, 1.4, "strip"),
    Shot("05_granny_wasd", 4.3, 1.6, "strip"),
    Shot("05_granny_wasd", 6.5, 1.5, "strip"),
    Shot("05_granny_wasd", 8.7, 1.6, "strip"),
    Shot("05_granny_wasd", 11.0, 1.4, "strip"),
    # solar smash — drop
    Shot("07_solar_touch", 0.3, 1.2, "strip"),
    Shot("07_solar_touch", 2.0, 1.4, "strip"),
    Shot("07_solar_touch", 4.6, 1.4, "strip"),
    Shot("07_solar_touch", 7.4, 1.4, "strip"),
    Shot("07_solar_touch", 10.4, 1.6, "strip"),
    Shot("07_solar_touch", 13.0, 1.5, "strip"),
    # utilities and out
    Shot("08_volume", 0.5, 2.2, "inset"),
    Shot("09_latency", 1.0, 2.0, "zoom"),
    Shot("10_clipboard", 3.0, 2.4, "full"),
    Shot("12_endcard", 2.0, 3.6, "full"),
]


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #


def vf_for(mode: str) -> str:
    if mode == "strip":
        # the tablet on a soft blow-up of itself — no dead black bars
        return (
            "[0:v]scale=1920:1080,boxblur=24:2,setsar=1[bg];"
            "[0:v]scale=-2:1080,setsar=1[fg];"
            "[bg][fg]overlay=(W-w)/2:0,format=yuv420p"
        )
    if mode == "zoom":
        # punch-in on the mirror area: chrome, toast and dock all leave frame
        return "crop=1462:822:0:180,scale=1920:1080:flags=lanczos,format=yuv420p"
    if mode == "inset":
        # near-full: drops the toast corner, keeps the status bar
        return "crop=1706:960:107:88,scale=1920:1080:flags=lanczos,format=yuv420p"
    return "pad=1920:1080:0:16:black,format=yuv420p"  # full


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def ffprobe_duration(path: Path) -> float:
    """ffmpeg-only duration probe (no ffprobe on this machine)."""
    p = run([str(FFMPEG), "-hide_banner", "-i", str(path), "-frames:v", "1", "-f", "null", "-"])
    for line in p.stderr.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            stamp = line.split(",")[0].split("Duration:")[1].strip()
            h, m, s = stamp.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    raise RuntimeError(f"no duration for {path}")


def snap(target: float, floor: float, grid: list[float]) -> float:
    cands = [g for g in grid if g >= floor]
    if not cands:
        return grid[-1]
    return min(cands, key=lambda g: abs(g - target))


# --------------------------------------------------------------------------- #
# plan: snap wanted durations onto the musical grid
# --------------------------------------------------------------------------- #


def plan() -> tuple[list[Shot], float, float]:
    beats = json.loads(BEATS.read_text(encoding="utf-8"))
    sec0, sec1 = beats["section"]
    grid = [c - sec0 for c in beats["section_cuts"]]
    span = sec1 - sec0

    shots: list[Shot] = []
    cursor, want_acc = 0.0, 0.0
    prev = 0.0
    for shot in SHOT_LIST:
        want_acc += shot.want
        boundary = snap(want_acc, prev + MIN_SHOT, grid)
        shot.at = prev
        shot.dur = round(boundary - prev, 4)
        shots.append(shot)
        prev = boundary

    total = prev
    if total < span:
        # hold the endcard to fill the section exactly
        shots[-1].dur = round(shots[-1].dur + (span - total), 4)
        total = span
    assert total <= span + 1e-6, f"shot list overruns the section by {total - span:.2f}s"

    # clamp in-points so a shot never runs past the end of its take
    cache: dict[str, float] = {}
    for shot in shots:
        key = str(shot.src())
        if key not in cache:
            if not shot.src().exists():
                raise RuntimeError(f"missing take: {shot.src()}")
            cache[key] = ffprobe_duration(shot.src())
        take_len = cache[key]
        if shot.inn + shot.dur > take_len - 0.05:
            shot.inn = max(0.0, take_len - shot.dur - 0.05)

    # float32-friendly boundaries
    t = 0.0
    for shot in shots:
        shot.at = round(t, 3)
        t += shot.dur
    return shots, sec0, span


def print_plan(shots: list[Shot], sec0: float, span: float) -> None:
    print(f"section {sec0:.2f} -> {sec0 + span:.2f}  ({span:.2f}s), {len(shots)} shots")
    print(f"{'#':>3} {'start':>7} {'dur':>6}  take                mode  in")
    for i, s in enumerate(shots, 1):
        print(f"{i:>3} {s.at:>7.2f} {s.dur:>6.2f}  {s.take:<20}{s.mode:<6}{s.inn:>6.2f}")
    total = sum(s.dur for s in shots)
    print(f"total {total:.2f}s  |  avg shot {total / len(shots):.2f}s")


# --------------------------------------------------------------------------- #
# render
# --------------------------------------------------------------------------- #


def render_shot(shot: Shot, idx: int) -> Path:
    out = SHOTS / f"s{idx:02d}.mp4"
    cmd = [
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
        "-ss", f"{shot.inn:.3f}", "-t", f"{shot.dur:.3f}",
        "-i", str(shot.src()),
        "-vf", vf_for(shot.mode),
        "-an", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-y", str(out),
    ]
    p = run(cmd)
    if p.returncode != 0 or not out.exists():
        raise RuntimeError(f"shot {idx} failed: {p.stderr[-500:]}")
    return out


def concat_shots(parts: list[Path], master: Path) -> None:
    lst = SHOTS / "concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    p = run([
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(lst),
        "-c", "copy", "-y", str(master),
    ])
    if p.returncode != 0:
        raise RuntimeError(f"concat failed: {p.stderr[-500:]}")


def mux_music(master: Path, sec0: float, total: float) -> None:
    track = None
    beats = json.loads(BEATS.read_text(encoding="utf-8"))
    track = Path(beats["track"])
    if not track.exists():
        raise RuntimeError(f"music missing: {track}")
    p = run([
        str(FFMPEG), "-hide_banner", "-loglevel", "error",
        "-i", str(master),
        "-ss", f"{sec0:.3f}", "-i", str(track),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-t", f"{total:.3f}", "-movflags", "+faststart",
        "-y", str(OUT),
    ])
    if p.returncode != 0:
        raise RuntimeError(f"mux failed: {p.stderr[-500:]}")


def stills(shots: list[Shot]) -> None:
    PREVIEW.mkdir(exist_ok=True)
    for i, s in enumerate(shots, 1):
        out = PREVIEW / f"s{i:02d}.png"
        run([
            str(FFMPEG), "-hide_banner", "-loglevel", "error",
            "-ss", f"{s.inn + s.dur / 2:.3f}", "-i", str(s.src()),
            "-frames:v", "1", "-y", str(out),
        ])
        print(f"still s{i:02d}  {s.take}")


def main() -> None:
    shots, sec0, span = plan()
    print_plan(shots, sec0, span)
    total = sum(s.dur for s in shots)
    if "--plan" in sys.argv:
        return
    if "--stills" in sys.argv:
        stills(shots)
        return
    SHOTS.mkdir(exist_ok=True)
    parts = [render_shot(s, i) for i, s in enumerate(shots, 1)]
    master = SHOTS / "master.mp4"
    concat_shots(parts, master)
    mux_music(master, sec0, total)
    size = OUT.stat().st_size
    print(f"wrote {OUT}  {size / 1e6:.1f} MB  {total:.2f}s")


if __name__ == "__main__":
    main()
