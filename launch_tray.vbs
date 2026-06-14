Dim shell, dir, pythonw, script
Set shell = CreateObject("WScript.Shell")

dir     = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
pythonw = dir & "\venv\Scripts\pythonw.exe"
script  = dir & "\tray.py"

shell.CurrentDirectory = dir
shell.Run """" & pythonw & """ """ & script & """", 0, False
