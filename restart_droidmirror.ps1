# Restart DroidMirror: close it if it is up, then launch it either way.
#
#     powershell -ExecutionPolicy Bypass -File restart_droidmirror.ps1
#
# Closing is a *request*, not a kill: the app tears down its adb forward and the
# scrcpy server on teardown, and a hard kill would leave both behind on the
# tablet for the next launch to trip over. So this asks the window to close and
# only forces the process if it is still there a few seconds later.
#
# A running app is normally *two* pythonw processes -- the window and the helper
# it spawns -- so seeing 2 here means one app, not two copies. They share a
# creation time, which is the quickest way to tell the pair apart from a genuine
# double-launch.

param(
    [int]$GraceSeconds = 10
)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Get-MirrorProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*droidmirror*' }
}

$running = @(Get-MirrorProcesses)
if ($running.Count -gt 0) {
    Write-Host "closing $($running.Count) process(es) (a single app is usually two): pid $($running.ProcessId -join ', ')"
    foreach ($p in $running) {
        try { (Get-Process -Id $p.ProcessId -ErrorAction Stop).CloseMainWindow() | Out-Null }
        catch { Write-Host "  pid $($p.ProcessId): no window to close" }
    }

    $deadline = (Get-Date).AddSeconds($GraceSeconds)
    while ((Get-Date) -lt $deadline -and @(Get-MirrorProcesses).Count -gt 0) {
        Start-Sleep -Milliseconds 250
    }

    $left = @(Get-MirrorProcesses)
    if ($left.Count -gt 0) {
        Write-Host "  still up after ${GraceSeconds}s -- forcing pid $($left.ProcessId -join ', ')"
        $left | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 500
    }
} else {
    Write-Host "not running"
}

# wscript hosts a .vbs with no console, which is the only launch that opens no
# window at all (see DroidMirror.vbs for why run.bat cannot manage that).
Start-Process -FilePath "wscript.exe" -ArgumentList "`"$root\DroidMirror.vbs`"" -WorkingDirectory $root

# The GUI is up only when one of the pair owns a window: a bare process can
# still be starting (or dying), and "a process exists" is not the same answer as
# "the window is on screen".
$deadline = (Get-Date).AddSeconds(30)
$up = $null
while ((Get-Date) -lt $deadline -and -not $up) {
    Start-Sleep -Milliseconds 500
    foreach ($proc in Get-MirrorProcesses) {
        $title = (Get-Process -Id $proc.ProcessId -ErrorAction SilentlyContinue).MainWindowTitle
        if ($title) {
            $up = "pid $($proc.ProcessId) -- window '$title'"
            break
        }
    }
}

if ($up) {
    Write-Host "up: $up"
} else {
    Write-Host "FAILED to come up with a window -- check crash.log"
    exit 1
}
