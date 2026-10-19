' Launch DroidMirror with no console window at all.
'
' Double-clicking a .bat makes Windows create a console *before* the first
' line of the script runs, so a .bat can close that window quickly but can
' never avoid showing it. WScript is a GUI host with no console of its own,
' so this file opens nothing -- not even a flash. That is the whole reason it
' exists alongside run.bat.
'
' Start it exactly like run.bat (double-click). Keep run.bat too if you ever
' want the console for debugging; use this one for everyday launching.
Option Explicit

Dim shell, here
Set shell = CreateObject("WScript.Shell")

' ScriptFullName is already absolute, so this works no matter what the
' current directory happens to be.
here = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
shell.CurrentDirectory = here

' 0 = hidden window, False = do not wait for it to exit.
shell.Run """" & here & ".venv\Scripts\pythonw.exe"" -m droidmirror", 0, False
