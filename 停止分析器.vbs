' ============================================================================
'  osumania_replay_analyser —— 停止本地服务
'
'  启动器现在把服务放在后台（没有控制台窗口可按 Ctrl+C 或直接关），
'  所以用这个脚本按命令行精确关掉正在跑 app\server.py 的 python 进程，
'  不会误伤别的 Python 程序。
' ============================================================================
Option Explicit

Dim wmi, procs, p, n, cmdline
n = 0
Set wmi = GetObject("winmgmts:\\.\root\cimv2")
Set procs = wmi.ExecQuery("select ProcessId, CommandLine from Win32_Process")

For Each p In procs
    ' CommandLine 对部分系统进程是 Null，直接 LCase 会报「无效使用 Null」
    cmdline = ""
    If Not IsNull(p.CommandLine) Then cmdline = LCase(CStr(p.CommandLine))
    ' 只认本项目的 app\server.py：光看 "server.py" 会把别的 Python 服务一起杀掉
    If (InStr(cmdline, "app\server.py") > 0 Or InStr(cmdline, "app/server.py") > 0) And InStr(cmdline, "python") > 0 Then
        On Error Resume Next
        p.Terminate
        If Err.Number = 0 Then n = n + 1
        Err.Clear
        On Error GoTo 0
    End If
Next

If n = 0 Then
    MsgBox "现在没有在跑的分析器服务。", 64, "osumania_replay_analyser"
Else
    MsgBox "已停止 " & n & " 个服务进程。", 64, "osumania_replay_analyser"
End If
