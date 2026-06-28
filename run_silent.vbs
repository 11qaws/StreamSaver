Set objShell = CreateObject("WScript.Shell")
Set objFSO   = CreateObject("Scripting.FileSystemObject")

strDir    = objFSO.GetParentFolderName(WScript.ScriptFullName)
strPython = strDir & "\venv\Scripts\pythonw.exe"
strScript = strDir & "\main.py"

objShell.CurrentDirectory = strDir
objShell.Run """" & strPython & """ """ & strScript & """", 0, False
