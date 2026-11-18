' Starts watchdog.py in the background (pythonw, no window) and passes every argument through.
'   run_watchdog.vbs --port 8641 -- --data "data"           watch the bot on 8641 (start it if needed)
'   run_watchdog.vbs --port 8641 --open -- --data "data"    same, and open the dashboard
'   run_watchdog.vbs --port 8641 --stop                                    stop that watchdog and its bot
' Everything after -- goes to stockbot.py. A second watchdog for the same port exits at once. See WATCHDOG.md.
Option Explicit
Dim sh, fso, dir, args, i, cmd, err1, f
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
args = ""
For i = 0 To WScript.Arguments.Count - 1
  args = args & " " & Quote(WScript.Arguments(i))
Next
sh.CurrentDirectory = dir
cmd = Quote(dir & "\watchdog.py") & args

On Error Resume Next
sh.Run "pythonw.exe " & cmd, 0, False
If Err.Number <> 0 Then
  err1 = Err.Description
  Err.Clear
  sh.Run "pyw.exe -3 " & cmd, 0, False          ' the py launcher, if pythonw.exe isn't on PATH
  If Err.Number <> 0 Then
    Set f = fso.OpenTextFile(sh.ExpandEnvironmentStrings("%TEMP%") & "\screenstocks-watchdog-error.txt", 8, True)
    f.WriteLine Now & "  couldn't start watchdog.py: pythonw.exe: " & err1 & " / pyw.exe: " & Err.Description
    f.Close
  End If
End If

' Quote one argument for the Windows command line (WScript.Arguments never contain a double quote, so only
' backslashes in front of the closing quote need doubling: "D:\Data\" must not turn into an escaped quote).
Function Quote(s)
  Dim n
  n = 0
  Do While n < Len(s)
    If Mid(s, Len(s) - n, 1) <> "\" Then Exit Do
    n = n + 1
  Loop
  Quote = """" & s & String(n, "\") & """"
End Function
