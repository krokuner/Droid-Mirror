"""Contract the one-file build relies on.

A frozen executable has no repository next to it: PyInstaller unpacks read-only
payload into ``sys._MEIPASS`` and deletes it on exit, so anything the app *writes*
has to resolve beside the .exe instead. A one-file build only works if these two
directories stay split, which is what these tests pin.
"""

from __future__ import annotations

import sys

import pytest

from droidmirror import adb as adb_mod
from droidmirror import config
from droidmirror.server import ensure_server_jar


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    """Simulate a one-file run: exe in one directory, bundle in another."""
    exe_dir = tmp_path / "portable"
    exe_dir.mkdir()
    bundle = tmp_path / "meipass"
    (bundle / "vendor").mkdir(parents=True)

    monkeypatch.delenv("DROIDMIRROR_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setattr(sys, "executable", str(exe_dir / "DroidMirror.exe"))
    return exe_dir, bundle


def test_written_state_lands_beside_the_executable(frozen):
    exe_dir, bundle = frozen

    assert config.project_root() == exe_dir
    assert config.bundle_root() == bundle
    assert config.profiles_dir() == exe_dir / "profiles"
    assert config.settings_path() == exe_dir / "settings.json"
    # both are created on demand, not shipped
    assert config.profiles_dir().is_dir()
    assert config.vendor_dir() == exe_dir / "vendor"


def test_shipped_server_jar_is_copied_out_of_the_bundle(frozen):
    exe_dir, bundle = frozen
    jar = bundle / "vendor" / config.SERVER_JAR_NAME
    jar.write_bytes(b"F" * 4096)

    resolved = ensure_server_jar()

    assert resolved == exe_dir / "vendor" / config.SERVER_JAR_NAME
    assert resolved.read_bytes() == jar.read_bytes()
    # the extraction directory is wiped on exit, so the copy must not point at it
    assert not str(resolved).startswith(str(bundle))


def test_bundled_adb_wins_over_a_writable_vendor_copy(frozen, monkeypatch):
    exe_dir, bundle = frozen
    monkeypatch.setattr(adb_mod.shutil, "which", lambda _name: None)

    for root in (exe_dir, bundle):
        pt = root / "vendor" / "platform-tools"
        pt.mkdir(parents=True)
        (pt / adb_mod._exe("adb")).write_bytes(b"fake")

    found = adb_mod.find_adb()

    assert found == str(bundle / "vendor" / "platform-tools" / adb_mod._exe("adb"))
