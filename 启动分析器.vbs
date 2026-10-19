' ============================================================================
'  osumania_replay_analyser —— 启动器（VBScript）
'
'  只做三件事：起本地服务 → 等服务就绪 → 打开一个网页。
'
'  和原来的 .bat 不一样的地方：
'    * 不再打开 _replays 文件夹（页面里本来就列着那份列表，另有「打开文件夹」按钮）；
'    * 网址不带随机数（?t=xxxx 是当年为了绕缓存加的，现在页面自带版本自愈，不需要了）；
'    * 全程没有黑色控制台窗口，后台静默起服务。
'  每一步都写日志到 _tools\_launcher.log —— 双击没反应时看这个文件就知道卡在哪一步。
'  服务进程没有可见窗口，要停请用「停止分析器.vbs」。
'
'  踩过的坑（别改回去）：健康检查原来用 MSXML2.XMLHTTP，它走 IE 的 WinINet，
'  会被系统代理拦下来（本机 127.0.0.1 也不一定绕过），结果就是"服务起来了但探测不到"，
'  脚本一直空等到超时、什么窗口都不弹。现在统一走 WinHTTP（不读 IE 代理设置）。
' ============================================================================
Option Explicit

Dim sh, fso, logFile, root, appPath, cacheDir, portFile, tmpDir
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(WScript.ScriptFullName)
appPath = root & "\app"
cacheDir = appPath & "\_cache"
portFile = cacheDir & "\port.txt"
tmpDir = fso.GetSpecialFolder(2)   ' 系统临时目录
logFile = root & "\_tools\_launcher.log"

Log "============================================================"
Log "启动器运行 " & Now()

If Not fso.FolderExists(root & "\_tools") Then fso.CreateFolder(root & "\_tools")
If Not fso.FolderExists(root & "\_replays") Then fso.CreateFolder(root & "\_replays")

' ------------------------------------------- 0) 已经有一个实例在跑？直接用它
Dim port
port = ""
If Alive("8806") Then
    port = "8806"
    Log "8806 上已经有服务在跑，直接打开网页"
    OpenPage port
    WScript.Quit 0
End If

' ---------------------------------------------------------------- 1) 找 Python
Dim pyCmd
pyCmd = FindPython()
Log "python = [" & pyCmd & "]"
If pyCmd = "" Then
    MsgBox "内置的运行环境不见了（python\python.exe）。" & vbCrLf & vbCrLf & _
           "可能是被删除或被安全软件隔离了：重新解压一份完整的程序包即可。" & vbCrLf & _
           "也可以自己装 Python 3.10+（安装时勾选 Add python.exe to PATH）。", 16, "osumania_replay_analyser"
    Log "没找到 Python，退出"
    WScript.Quit 1
End If

' ------------------------------------------------- 2) 清掉上一次的端口记录
If fso.FileExists(portFile) Then fso.DeleteFile portFile, True
If Not fso.FolderExists(cacheDir) Then fso.CreateFolder cacheDir

' ---------------------------------------------------------------- 3) 起服务
Dim cmdline
cmdline = pyCmd & " " & Quote(appPath & "\server.py") & " --port 8806 --no-open"
Log "启动服务：" & cmdline
sh.CurrentDirectory = root
On Error Resume Next
sh.Run cmdline, 0, False
If Err.Number <> 0 Then
    Log "sh.Run 失败：" & Err.Description
    MsgBox "没能启动服务进程：" & Err.Description, 16, "osumania_replay_analyser"
    WScript.Quit 1
End If
On Error GoTo 0

' ------------------------------------------------------- 4) 等服务真的能用
port = WaitPortFile(portFile, 30)          ' 最多等 15 秒拿到端口
Log "port.txt = [" & port & "]"
If port = "" Then port = "8806"
If Not WaitHealth(port, 30) Then           ' 再多等 15 秒，等 /api/health 有应答
    Log "健康检查超时（端口 " & port & "）"
    ' 探测不到不代表服务没起来（见文件头的坑），照样把网页打开，让人自己看
    OpenPage port
    MsgBox "没能确认服务就绪（端口 " & port & "），但已经试着打开网页了。" & vbCrLf & _
           "如果页面打不开，手动跑一次看报错：" & vbCrLf & pyCmd & " app\server.py", _
           48, "osumania_replay_analyser"
    WScript.Quit 1
End If
Log "健康检查通过，端口 " & port

' ---------------------------------------------------------------- 5) 开网页
OpenPage port
Log "已调用打开网页，结束"
WScript.Quit 0

' ============================================================================
Sub OpenPage(p)
    Dim url
    url = "http://127.0.0.1:" & p & "/"
    Log "打开 " & url
    On Error Resume Next
    sh.Run url, 1, False
    If Err.Number <> 0 Then
        Log "sh.Run(url) 失败：" & Err.Description & "，改用 cmd start"
        Err.Clear
        sh.Run "cmd /c start """" " & Quote(url), 0, False
        If Err.Number <> 0 Then
            Log "cmd start 也失败：" & Err.Description
            MsgBox "服务已经起来了，但打不开浏览器，请手动访问：" & vbCrLf & url, _
                   48, "osumania_replay_analyser"
        End If
    End If
    On Error GoTo 0
End Sub

Sub Log(msg)
    On Error Resume Next
    Dim st
    Set st = fso.OpenTextFile(logFile, 8, True)   ' 8 = ForAppending
    If Err.Number = 0 Then
        st.WriteLine FormatDateTime(Now(), 4) & "  " & msg
        st.Close
    End If
    Err.Clear
    On Error GoTo 0
End Sub

Function Quote(s)
    Quote = """" & s & """"
End Function

' 返回可直接拼进命令行的 Python：优先项目自带的便携版，其次 py -3 / python
Function FindPython()
    Dim p
    p = root & "\python\python.exe"
    If fso.FileExists(p) Then
        FindPython = Quote(p)
        Exit Function
    End If
    p = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%\Programs\Python\Python314\python.exe")
    If fso.FileExists(p) Then
        FindPython = Quote(p)
        Exit Function
    End If
    If HasCommand("py") Then
        FindPython = "py -3"
        Exit Function
    End If
    If HasCommand("python") Then
        FindPython = "python"
        Exit Function
    End If
    FindPython = ""
End Function

' where 命令能找到就算有（结果写临时文件，避免弹出控制台）
Function HasCommand(name)
    Dim out
    out = tmpDir & "\mania_where_" & name & ".txt"
    HasCommand = False
    On Error Resume Next
    sh.Run "cmd /c where " & name & " > """ & out & """ 2>nul", 0, True
    Err.Clear
    On Error GoTo 0
    If Not fso.FileExists(out) Then Exit Function
    If fso.GetFile(out).Size <= 0 Then Exit Function
    HasCommand = True
End Function

' 某个端口上是不是已经有能用的服务
Function Alive(p)
    Alive = Http200("http://127.0.0.1:" & p & "/api/health", 1)
End Function

Function WaitPortFile(path, tries)
    Dim i, ts
    WaitPortFile = ""
    For i = 1 To tries
        If fso.FileExists(path) Then
            ts = ReadAllText(path)
            If Len(ts) > 0 Then
                WaitPortFile = ts
                Exit Function
            End If
        End If
        WScript.Sleep 500
    Next
End Function

' 等 /api/health 返回 200
Function WaitHealth(p, tries)
    Dim i
    WaitHealth = False
    For i = 1 To tries
        If Http200("http://127.0.0.1:" & p & "/api/health", 1) Then
            WaitHealth = True
            Exit Function
        End If
        WScript.Sleep 500
    Next
End Function

' 健康检查：优先 WinHTTP（不读 IE 代理设置），不行再退到 ServerXMLHTTP / XMLHTTP
Function Http200(url, secs)
    Dim http, ms
    Http200 = False
    ms = secs * 1000

    On Error Resume Next
    Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
    If Err.Number = 0 Then
        http.Open "GET", url, False
        http.SetTimeouts 800, 800, ms, ms
        http.Send
        If Err.Number = 0 Then
            If http.Status = 200 Then
                Http200 = True
                Err.Clear
                On Error GoTo 0
                Exit Function
            End If
        End If
        Err.Clear
    Else
        Err.Clear
    End If

    Set http = CreateObject("MSXML2.ServerXMLHTTP.6.0")
    If Err.Number = 0 Then
        http.Open "GET", url, False
        http.setTimeouts 800, 800, ms, ms
        http.Send
        If Err.Number = 0 Then
            If http.Status = 200 Then
                Http200 = True
                Err.Clear
                On Error GoTo 0
                Exit Function
            End If
        End If
        Err.Clear
    Else
        Err.Clear
    End If

    Set http = CreateObject("MSXML2.XMLHTTP")
    If Err.Number = 0 Then
        http.Open "GET", url, False
        http.setTimeouts 800, 800, ms, ms
        http.Send
        If Err.Number = 0 Then
            If http.Status = 200 Then Http200 = True
        End If
    End If
    Err.Clear
    On Error GoTo 0
End Function

' 服务自己的日志最后几行（起不来时最有用的线索）
Function ServerLogTail()
    Dim path, st, lines, i, out
    path = cacheDir & "\server.log"
    ServerLogTail = ""
    On Error Resume Next
    If Not fso.FileExists(path) Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    Set st = fso.OpenTextFile(path, 1, False)
    If Err.Number <> 0 Then
        Err.Clear
        On Error GoTo 0
        Exit Function
    End If
    lines = Split(st.ReadAll(), vbCrLf)
    st.Close
    On Error GoTo 0
    out = "服务日志最后几行："
    For i = UBound(lines) - 6 To UBound(lines)
        If i >= 0 Then out = out & vbCrLf & lines(i)
    Next
    ServerLogTail = out
End Function

Function ReadAllText(path)
    Dim st
    ReadAllText = ""
    On Error Resume Next
    Set st = fso.OpenTextFile(path, 1, False)
    If Err.Number = 0 Then
        If Not st.AtEndOfStream Then ReadAllText = Trim(st.ReadLine())
        st.Close
    End If
    Err.Clear
    On Error GoTo 0
End Function
