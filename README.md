# DroidMirror

Low-latency Android screen mirroring over **USB**, with an on-screen **keybind
mapper**: bind a keyboard key to any coordinate on the phone screen and it
injects a real touch there.

Two things make it fast:

- **No frame queue anywhere.** Encoded packets are decoded in arrival order and
  the renderer only ever picks up the *newest* decoded frame. Nothing is
  buffered on the hot path. (FFmpeg's own frame-threaded decoding does hold a
  small bounded pipeline internally — two threads — and frames it returns that
  we don't need are dropped rather than rendered. So: no *unbounded* queue of
  ours, and never a stale frame on screen.)
- **USB transport with a hardware-encoded stream.** The device side is the
  stock [scrcpy](https://github.com/Genymobile/scrcpy) server, which encodes the
  screen with MediaCodec H.264 and writes it straight to a forwarded socket.

Rendering prefers GPU (Y/U/V uploaded as three textures, colour conversion in a
fragment shader) and falls back to a CPU path where GL is unavailable.

**No Python on the target machine?** Grab the installer from
[Releases](../../releases/latest) — `DroidMirror-Setup.exe` installs the whole
app (Python, Qt, FFmpeg and `adb` inside one file) and puts a shortcut on the
desktop.

---

## Requirements

| | |
|---|---|
| Phone | Android, with **USB debugging** enabled |
| Host | Windows / macOS / Linux |
| Tooling | [Android platform-tools](https://developer.android.com/studio/releases/platform-tools) (`adb`) |
| Runtime | Python 3.11+ |

The correct `scrcpy-server` jar is downloaded automatically on first run and
cached in `vendor/`.

## Install

```bash
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\pip install -e .
```

### Prebuilt installer (nothing to install first)

`DroidMirror-Setup.exe` from [Releases](../../releases/latest) contains Python,
Qt, FFmpeg and the Android platform-tools in one file. It installs to
`C:\ProgramData\DroidMirror`, offers a desktop shortcut, and needs no Python,
no `pip` and no `adb` on the machine. It is unsigned, so SmartScreen warns once
(*More info* → *Run anyway*).

The installer is a prebuilt artifact; there is no packaging pipeline in this
repository, so the clone-and-run path above is the way to build from source.

## Verify your setup

```bash
python -m droidmirror --check
```

Checks for `adb`, a usable device, and the server jar:

```
droidmirror, targeting scrcpy server 4.1
ok    adb: /usr/local/bin/adb
ok    adb version: Android Debug Bridge version 1.0.41
ok    device: Pixel 7 (2B091FDH3006GX) [device]
ok    server jar: vendor/scrcpy-server-v4.1
All good.
```

## Run

```bash
python -m droidmirror                       # native resolution
python -m droidmirror --max-size 1920       # cap resolution
python -m droidmirror --bit-rate 32000000   # crisper motion
python -m droidmirror --serial <SERIAL>     # pick a device
python -m droidmirror --profile mygame      # load keybinds on start
```

Then press **Connect**. **Refresh** re-reads the device list; **Add** pairs and
connects a device that is not the one already listed — type its `ip:port`, plus
its pairing code on Android 11+. Add is Wi-Fi only: over USB a device appears in
the list the moment it is plugged in, so the button is greyed out and does
nothing there. The Wi-Fi transport button only ever finds the device the app
already knows about, which is what made a second tablet or phone impossible to
bring in.

On Windows, launch it with **`DroidMirror.vbs`** — it opens no console window at
all, not even a flash (use `run.bat` if you want a console for debugging). To
bounce a running copy:

```powershell
powershell -ExecutionPolicy Bypass -File restart_droidmirror.ps1
```

That closes it if it is up and launches it either way. Closing is a request, not
a kill, so the app still gets to tear down its `adb forward` and the scrcpy
server on the tablet rather than leaving them behind for the next launch.

### Switching between mouse and keybinds

Press the **`` ` ``** (backtick) key at any time to flip which input drives the
phone:

- **`input: mouse`** (default) — click and drag to drive the device. Keybinds
  are **off**, so a stray keypress can never fire a touch while you are
  navigating menus.
- **`input: keybinds`** — your bindings are live. The mouse is **inert**, so
  clicking the mirror cannot inject an accidental tap while you play.

The current mode is shown in the status bar and mirrored by the **Keybind
control** toolbar button (clicking it does the same thing as the hotkey).

Switching never leaves a finger stuck down: any held binding or in-flight mouse
click is released first.

The backtick key is reserved for this toggle, so it cannot be bound to a screen
point.

### Camera drag (`x`)

Games that rotate the camera expect a **held touch-drag**, which normally means
holding the left button and shoving the mouse until it hits the edge of your
screen. Press **`x`** instead:

1. Move the pointer over the mirror, wherever you like.
2. Press **`x`**. The pointer is **hidden and parked at the centre of the
   mirror**, and a camera drag is planted on the profile's **Camera Movement**
   spot and held down for you.
3. Move the mouse. The camera turns, **through a full 360°**, in any direction —
   there is no screen edge to run into, because the pointer never travels.

Holding the left button yourself works too: it is the same drag, and the app
just adds the movement to it. Press **`x`** again to release the drag and get
the cursor back.

An explicit `x` keybind takes precedence over the shortcut — bind `x` to a
screen point and the camera drag stays out of your way.

Note that where the pointer happens to be when you press `x` does **not**
matter: the drag always plants on the same spot, so the lock behaves
identically every time. Move that spot with *Edit keybinds → Set position*, and
freeze it with *Lock* so it can never be nudged while you arrange other
bindings. It defaults to the middle of the screen.

`x` also stays out of your typing: while the tablet's **on-screen keyboard is
open**, pressing `x` is ignored rather than stealing the mouse into a camera
lock. An explicit `x` binding still wins, because that is an explicit ask for
a touch.

### Hotkeys

| Key | Does |
|---|---|
| `` ` `` | switch mouse / keybind control |
| `x` | camera drag (mouse-look) |
| `F7` | show / hide the keybind markers (pad and camera spot too) |
| `F9` | latency HUD |
| `F11` | fullscreen (`Esc` exits) |
| `Ctrl+V` / `Ctrl+Shift+V` | paste from this computer / from the device |

Hiding markers with `F7` is purely cosmetic — bindings, the WASD pad and the
camera spot keep working, the screen just stops drawing them. Handy for a clean
view while you watch or record.

### Scroll wheel

Scrolling the mouse wheel flicks the screen. Each notch is sent as a short
finger drag at the cursor — DOWN, a beat, MOVE + UP — so anything that responds
to a finger responds to this. Fast spins coalesce into longer strokes rather
than teleporting the view.

Only the vertical axis is used; horizontal wheel/tilt is ignored. It works in
both control modes (a wheel notch involves no held button, so there is nothing
for the mouse/keybind split to guard against), but not while *Edit keybinds* is
on — arranging markers should not scroll lists.

### Sound

Device audio plays on this computer. **Mute** in the toolbar silences it; the
stream keeps running, so un-muting is instant and never re-connects. The choice
is remembered for the next connection.

The status bar carries the **tablet's own** media volume — a slider and a pair
of `-`/`+` buttons, all three changing the device itself rather than this
computer's playback:

- Both send the tablet's own volume key over the control socket, so they land
  immediately and the tablet raises its volume panel, exactly as its side
  buttons would. `-`/`+` is one step; the slider is replayed as one step per
  index it moved, so it tracks your finger. Measured on a Galaxy Tab S9 FE: a
  three-step drag costs **0.4 ms** end to end, versus the absolute
  `cmd media_session volume` write it replaced — that was **193 ms**, sitting
  on an adb round trip behind a 180 ms debounce, and it never made the tablet
  show anything.
- Clicking the bar lands on the point clicked, and a drag carries on from
  there. A plain Qt slider answers a press in the groove with a ten-step page
  jump — on the tablet's own 0..15 range that dropped every click left of the
  handle to 5, wherever it was aimed.
- A key only says *up* or *down*, so nothing reports the new level back. A
  moment after the changes stop, the level is read once and the slider is put
  back in line. If that reading disagrees with the number you dragged to, the
  tablet was not where it looked (its own buttons, or another app, moved it),
  and the number is then written exactly — once, so a write that does not take
  cannot loop.
- With no reading the slider is disabled and shows `-`. A made-up `0/15` would
  claim you had turned the tablet down.

---

## Binding keys to the screen

1. Press **Add binding**. A recorder pops up and waits for one key press —
   what it catches is shown before it closes, so there is no guessing.
   (`Esc` cancels; a lone `Shift`/`Ctrl`/`Alt` does not count.)
2. Click the point on the phone screen the key should tap, or drag an existing
   marker to reposition it.

The WASD pad gets a **Joystick size** slider once it exists, and its rim is a
drag handle — drag the ring to grow or shrink the pad, drag the middle to move
it.

*Edit keybinds* is independent of the mouse/keybind toggle: while it is on, the
mouse places markers instead of driving the device, and it remembers which
input target you had selected when you leave.

Each binding has a mode:

- **tap** — a quick down+up at the point (buttons, skills, menu items).
- **hold** — the finger stays down until you release the key (movement,
  joysticks, "press and keep pressing").

Double-click a row, or use **Toggle tap/hold**, to switch. Every binding gets
its own touch pointer id, so **several can be held at once** — a WASD-style
cluster plus an action key works as true multitouch.

Positions are stored **normalised** (0–1), not in pixels, so bindings survive
rotation and resolution changes. Profiles are plain JSON in `profiles/`,
auto-saved as you edit.

### Modes

There are two independent switches, not one:

- **Input target** (`input: mouse` / `input: keybinds`, toggled with `` ` ``) —
  decides whether the mouse or your keybindings drive the phone. This is the
  *default* state, so keys feed the keymapper while the mouse drives the phone
  directly.
- **Edit keys** (a separate toolbar toggle) — keys get armed for binding
  instead of being injected. It is independent of the input target: turning it
  on makes the mouse place markers instead of driving the device, and leaves
  the input target as you had it.

---

### Discord Rich Presence

The **Discord** toolbar button advertises what you are actually doing — the app
in the foreground on the tablet, straight from the device's own `dumpsys`:

| On the tablet | Discord shows |
|---|---|
| YouTube | **YouTube**, with its real icon |
| Roblox | **Roblox**, with its real icon |
| the launcher | **Using mobile phone**, with its own picture |
| an unrecognised package | its last segment, e.g. *Thing* |

Icons are the app's own, found online and handed to Discord as a **url** that
Discord then fetches itself (see *Why a url* below). An app whose icon cannot
be found goes out with no image at all, and Discord falls back to the
application's own icon rather than to a placeholder.

The home screen has no app behind it, so it gets a fixed picture of a phone,
`discord_rpc.IDLE_IMAGE_URL`. That picture is built from `idle-screen.jpg` by
`make_idle_square.py`, which does two things the raw file cannot survive: pads it
out to a 512×512 square — the card draws large art as a rounded square and
centre-crops whatever else it is handed, so at 360×696 the phone lost its top and
bottom — and punches its paper background out to **transparency**, because a
white tile on the card's dark background reads as a broken image. To change the
picture, replace `idle-screen.jpg`, re-run that script, upload
`icons/idle-screen-square.png`, and point the constant at the new url.

It needs a Discord **Application ID** (create one at
[discord.com/developers/applications](https://discord.com/developers/applications);
the id is on *General Information*). Click **Discord** and paste it when
asked — it is stored in `settings.json` and never sent anywhere but Discord.

**No token, and no login credential.** Icons used to be pulled off the tablet
and uploaded to Discord, which needed your Discord *user token* — your whole
account — written into `settings.json` in plain text. That is gone, and those
uploads never worked anyway: the client discards an uploaded asset reference
and paints "?" over the app's own icon. Everything works with nothing but the
application id.

Each app's icon is looked up **once** and then reused forever
(`icons/discord_assets.json`). Two sources, tried in order:

1. **Google Play.** The store page carries the app's real launcher icon as an
   `og:image` tag, at 512×512 — the *actual* icon the phone draws, not a
   lookalike. Only the package id is sent, and only the *url* of the image is
   kept: the picture itself is never downloaded.
2. **The app's website favicon**, via Google's public favicon service, for
   anything Play does not have — but only once that favicon has been fetched
   and confirmed to exist, because a url that 404s draws as a broken image, and
   that is worse than drawing nothing. Only the *domain* is sent, never the
   package id.

Play first is what makes obfuscated builds work at all: a modern release APK
renames its resources (`res/-e.png`), so there is no `ic_launcher` left on the
device to find.

Anything not found falls through — no icon is ever guessed, because the wrong
picture on your profile is worse than none.

Measured on a real tablet: **67 of 113 installed apps resolve from the web**
(Instagram, YouTube, Roblox, Discord, VLC, Zoom, Spotify…), each in ~200 ms,
each only ever once. The rest go out imageless.

#### Why a url, and not an uploaded asset

Discord's docs say an uploaded asset is referenced as
`large_image = {application_asset_id}`. That is not what its desktop client
accepts. Measured against the running client, which echoes back the value it
kept — a url comes back rewritten as `mp:external/<hash>/https/…`, which is the
client reporting that it fetched and proxied the image:

| `large_image` sent | echoed back |
|---|---|
| `https://play-lh.googleusercontent.com/…` | `mp:external/e5DF1EE11Do1…/https/…` |
| `1560714379794579618` (an asset id) | *dropped* |
| `com-instagram-android-e9d73a12` (an asset key) | *dropped* |

A dropped value is worse than no value: Discord then paints its grey "?" over
the whole card instead of falling back to the application's own icon. So
`discord_rpc.is_image_reference` accepts a url and refuses every other shape,
and a cached value that is not a url is treated as a miss and looked up again.

The upload half of `appicons.py` — the APK reader, the uploader and the two
drawn glyphs — is still in the tree, but nothing calls it any more. It is not
under version control, so deleting it is a deliberate step rather than a
by-product of fixing the icon.

> If you use the same Discord **application id** in two tools, they will fight
> over the presence — Discord keys presence by application, not by client, so
> whichever ticks last wins. Give each tool its own id from the developer
> portal.

Without `pypresence`, Discord closed, or no application id, the whole feature
is inert: nothing is logged and nothing on the mirror changes.

---

## How it works

DroidMirror speaks the scrcpy client protocol directly, which is what allows
touch injection at arbitrary coordinates. `src/droidmirror/protocol.py` is
implemented against the scrcpy **v4.1** sources (client and server must match
versions exactly — the server refuses to run otherwise):

| Piece | Source of truth |
|---|---|
| Control message layouts | `app/tests/test_control_msg_serialize.c` |
| Message type numbering | `app/src/control_msg.h` |
| Codec id / session / frame framing | `server/.../device/Streamer.java` |
| 64-byte device name field | `server/.../device/DesktopConnection.java` |
| Socket + handshake order | `server/.../Server.java` |

With a forward tunnel the video socket carries, in order:

```
1 dummy byte -> 64-byte device name -> u32 codec id -> session packets + media packets
```

and every media packet is prefixed by a 12-byte header whose top bits are
flags (`1<<63` session, `1<<62` config, `1<<61` keyframe) with a 61-bit PTS.

Touches are injected as `INJECT_TOUCH_EVENT` with `pressure = 0.0` for
`ACTION_UP` and `1.0` otherwise, mirroring scrcpy's own client.

### Layout

```
src/droidmirror/
  protocol.py     scrcpy wire protocol (control + video demuxer)  <- pure, no I/O
  adb.py          adb discovery, devices, forwarding
  server.py       server jar download/push/launch + handshake
  session.py      reader thread, control channel, newest-frame handoff
  decoder.py      PyAV H.264 -> owned YUV planes
  color.py        YUV -> RGB (CPU fallback path)
  keymap.py       Binding/Profile model + injection engine  <- pure, no I/O
  ui/keys.py      canonical key names                        <- pure, no I/O
  ui/layout.py    letterbox geometry                         <- pure, no I/O
  ui/glview.py    GL shader renderer + overlay editor
  ui/main_window.py
```

---

## Tests

```bash
.venv/bin/python -m pytest          # Windows: .venv\Scripts\python -m pytest
```

All 490 tests run **without a phone** (13 live-device checks are skipped when none is attached):

- control messages are asserted byte-for-byte against scrcpy's own vectors,
- the demuxer is fuzzed across random and byte-at-a-time chunk boundaries,
- a real H.264 stream (generated with ffmpeg) is wrapped in scrcpy framing and
  pushed through the demuxer and decoder, checking luma byte-exactness, plane
  layout, and that a solid orange frame reconstructs as orange (which would
  catch a U/V swap),
- `VideoDecoder.flush()`'s output shape is verified against the decoder's own
  planes,
- the GUI is exercised under Qt's `offscreen` platform, covering the full
  key-event → touch-injection path.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `No Android device connected over USB` | Connect the cable, set USB mode to *File transfer*, re-run `--check` |
| `Device is unauthorized` | Accept the **Allow USB debugging** prompt on the phone |
| `adb not found` | Install platform-tools, or set `ANDROID_HOME` |
| Black screen, `render: CPU` | Your GPU/driver refused a GL context; the CPU path still works, just slightly higher latency |
| Keys don't register in the game | Try switching that binding to **hold**; some games ignore very short taps |
| `Could not download ...` | No network on first run — download `scrcpy-server-v4.1` from the scrcpy releases page into `vendor/` |
