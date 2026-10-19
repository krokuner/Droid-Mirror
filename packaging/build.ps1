# Build dist\DroidMirror.exe from a clean checkout.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -Console
#
# -Console builds the version that prints to the terminal, for diagnostics.

[CmdletBinding()]
param(
    [switch]$Console
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "No virtualenv at .venv -- create one and 'pip install -e .' first."
}

& $python -m pip install --disable-pip-version-check -q pyinstaller pillow

if ($Console) { $env:DROIDMIRROR_CONSOLE = "1" } else { Remove-Item Env:\DROIDMIRROR_CONSOLE -ErrorAction SilentlyContinue }

& $python -m PyInstaller --clean --noconfirm packaging\DroidMirror.spec

$exe = Join-Path $root "dist\DroidMirror.exe"
if (-not (Test-Path $exe)) { throw "Build produced no executable." }

$mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "built $exe ($mb MB)"
