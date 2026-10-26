' 静默启动 SeeThroughWindow（不显示控制台窗口）
Dim shell, fso, here
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here
shell.Run "pythonw.exe """ & here & "\see_through_window.py""", 0, False
