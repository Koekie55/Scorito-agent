' run-hidden.vbs
' Launches the command line passed as arguments with no visible window, waits
' for it to finish, and returns its exit code. Used by the Scorito scheduled
' tasks so their PowerShell runners no longer pop up console windows while
' keeping execution-time-limits, failure retries and last-run-result intact.
Option Explicit
Dim shell, i, arg, cmd, rc
Set shell = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
    arg = WScript.Arguments(i)
    If InStr(arg, " ") > 0 And Left(arg, 1) <> """" Then
        arg = """" & arg & """"
    End If
    If Len(cmd) > 0 Then cmd = cmd & " "
    cmd = cmd & arg
Next
If Len(cmd) = 0 Then WScript.Quit 2
rc = shell.Run(cmd, 0, True)
WScript.Quit rc
