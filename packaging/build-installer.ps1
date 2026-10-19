# Build dist\DroidMirror-Setup.exe -- the install wizard.
#
#   powershell -ExecutionPolicy Bypass -File packaging\build-installer.ps1
#
# Produces a wizard that installs DroidMirror to C:\ProgramData\DroidMirror,
# grants Users write access there (the app stores its profiles beside the exe),
# and offers a desktop shortcut. Requires dist\DroidMirror.exe to exist already
# -- run packaging\build.ps1 first.

[CmdletBinding()]
param(
    [string]$Iscc
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path "dist\DroidMirror.exe")) {
    throw "dist\DroidMirror.exe missing -- run packaging\build.ps1 first."
}

if (-not $Iscc) {
    $Iscc = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
    if (-not (Test-Path $Iscc)) {
        $Iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    }
}
if (-not (Test-Path $Iscc)) {
    throw "ISCC.exe not found. Install Inno Setup 6:  winget install JRSoftware.InnoSetup --override `"/VERYSILENT /CURRENTUSER`""
}

& $Iscc "packaging\DroidMirror.iss"

$setup = Join-Path $root "dist\DroidMirror-Setup.exe"
if (-not (Test-Path $setup)) { throw "Compile produced no installer." }

$mb = [math]::Round((Get-Item $setup).Length / 1MB, 1)
Write-Host "built $setup ($mb MB)"
