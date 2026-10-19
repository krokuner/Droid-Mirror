# Building DroidMirror as one executable

```powershell
# from the repository root, with the project venv active
.venv\Scripts\python.exe -m pip install pyinstaller pillow
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm packaging\DroidMirror.spec
```

`dist\DroidMirror.exe` is the whole app — Python, Qt, FFmpeg (via PyAV),
OpenGL bindings and `adb` inside one file. Copy it anywhere and double-click it;
nothing needs to be installed on the target machine.

For a version that prints to a terminal (diagnostics):

```powershell
$env:DROIDMIRROR_CONSOLE = "1"
.venv\Scripts\python.exe -m PyInstaller --clean --noconfirm packaging\DroidMirror.spec
.\dist\DroidMirror.exe --check
```

## What ships inside, and what lives beside the .exe

| Location | Holds | Why |
|---|---|---|
| inside the bundle (`sys._MEIPASS`) | `vendor/scrcpy-server-v4.1`, `vendor/platform-tools/adb.exe` + its two DLLs, all Python/Qt/FFmpeg code | read-only; the one-file extraction directory is deleted when the app exits |
| beside `DroidMirror.exe` | `settings.json`, `profiles/`, `icons/`, `vendor/scrcpy-server-v4.1`, `crash.log` | the app writes these, so they must survive a restart |

`droidmirror.config.project_root()` returns the executable's folder when
`sys.frozen` is set, and `bundle_root()` returns `sys._MEIPASS`; every path in
the app is derived from one of those two.

Two consequences worth knowing:

* **Run it from a folder you can write to.** Next to `Program Files` the app
  cannot save settings or profiles. Portable folder, Desktop, a USB stick — all
  fine. `DROIDMIRROR_HOME` overrides the location if you need it elsewhere.
* **The bundled `adb` starts a daemon that outlives the app.** The daemon is
  launched from the temporary extraction directory, so once the app closes, that
  daemon is still running from a path that no longer exists. It keeps working
  (the client talks to it over port 5037) but it is worth knowing:
  `adb kill-server` clears it.

## The installer

```powershell
winget install JRSoftware.InnoSetup --override "/VERYSILENT /CURRENTUSER"
powershell -ExecutionPolicy Bypass -File packaging\build.ps1            # dist\DroidMirror.exe
powershell -ExecutionPolicy Bypass -File packaging\build-installer.ps1  # dist\DroidMirror-Setup.exe
```

`packaging/DroidMirror.iss` is the wizard: installs to `C:\ProgramData\DroidMirror`,
adds a desktop shortcut (optional page), a Start Menu folder, a proper uninstall
entry, and asks on uninstall whether to keep your profiles.

Two things in it are load-bearing rather than cosmetic:

* `[Dirs] ... Permissions: users-modify` — the app saves everything beside its
  own executable, and Program Files refuses that. Without the ACL a machine-wide
  install would silently fail to save profiles.
* `DefaultDirName={code:GetDefaultDir}` — ProgramData for a normal (elevated)
  run, `%LOCALAPPDATA%\Programs` for `/CURRENTUSER`, so a user who cannot
  elevate still gets a working install.

Uninstalling asks "keep your profiles and settings?"; a *silent* uninstall
(`/VERYSILENT`) always keeps them, since a silent run cannot ask.

Testing an installer without touching the real machine:

```powershell
.\dist\DroidMirror-Setup.exe /VERYSILENT /CURRENTUSER /DIR=$env:TEMP\dm-test /LOG=$env:TEMP\dm.log
& "$env:TEMP\dm-test\unins000.exe" /VERYSILENT
```

If `ISCC` fails with `EndUpdateResource failed (110)`, something — almost always
the antivirus — still holds the previous `dist\DroidMirror-Setup.exe`. Delete it
and compile again.

## Other platforms

The same spec builds on the host it is run on — PyInstaller does not
cross-compile. On macOS use `packaging/DroidMirror.icns` for the icon and
`--windowed` produces a `.app`; a Linux build assumes the target has glibc at
least as new as the build machine and the usual X11/xcb client libraries, since
those are system components rather than Python packages.
