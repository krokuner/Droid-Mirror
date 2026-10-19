@echo off
rem Navigate to the directory where this batch file is located
cd /d "%~dp0"

rem Launch through pythonw.exe (the GUI-subsystem interpreter). Unlike
rem python.exe it never allocates a console, so the app cannot hold a command
rem prompt open for the length of the session.
rem
rem `start ""` is what lets THIS script return immediately. Executing the
rem interpreter directly makes cmd wait for the app to exit, and this
rem console would then sit on screen until the app was closed -- which is the
rem lingering window this replaces.
rem
rem Nothing is lost by having no console: crashlog.py already writes every
rem unhandled error, and native faults via faulthandler, to crash.log. That
rem file exists precisely because the console used to vanish with the process.
start "" "%~dp0.venv\Scripts\pythonw.exe" -m droidmirror

rem `start` returns at once, so this console closes as soon as the app is
rem handed off rather than lasting as long as the app does. (A .bat cannot
rem avoid the brief flash entirely: Windows creates the console before the
rem first line runs. Use DroidMirror.vbs for a launch with no window at all.)
exit /b 0