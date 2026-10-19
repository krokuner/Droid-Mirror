' Launch a promo tool with no window at all.
'
' A take is recorded off the desktop, so a console flashing up part-way through
' lands in the file -- and steals keyboard focus while it does. wscript has no
' window, and Run(..., 0, False) hides the process's own console, so a take can
' record while the machine looks untouched.
'
'     wscript promo\hidden.vbs shoot.py 01_connect
'     wscript promo\hidden.vbs shoot.py all
'
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "C:\Users\Hardik\Desktop\emulator type"

script = WScript.Arguments(0)
rest = ""
For i = 1 To WScript.Arguments.Count - 1
    rest = rest & " """ & WScript.Arguments(i) & """"
Next

shell.Run """" & shell.CurrentDirectory & "\.venv\Scripts\pythonw.exe"" ""promo\" & script & """" & rest, 0, False
