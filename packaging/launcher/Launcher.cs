// osumania_replay_analyser —— 启动器（无控制台窗口的小 exe）
//
// 做的事情与旧的 启动分析器.vbs 完全一致：
//   看看是不是已经在跑 → 起本地服务 → 等 /api/health 有应答 → 打开浏览器。
//
// 为什么换成 exe：.vbs 要经过脚本宿主，从网上下载后带「来自网络」标记会被 Windows
// 拦一道，杀毒软件对「脚本宿主 + 隐藏进程 + 发 HTTP」这套动作也特别敏感。exe 不走
// 脚本宿主，少一层麻烦，体积只有十几 KB（用系统自带的 .NET 编译器构建）。
//
// 踩过的坑（别改回去）：健康检查必须绕开系统代理。原来的 .vbs 用 MSXML2.XMLHTTP，
// 它走 IE 的 WinINet 设置，被代理拦下来就探测不到本机服务，于是一直空等到超时。
// 这里用 HttpWebRequest 并把 Proxy 显式设成 null，等价于 WinHTTP 的「不读 IE 代理」。

using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;

static class Launcher
{
    const string AppName = "osumania_replay_analyser";
    const int StartPort = 8806;

    const uint MB_ICONERROR = 0x10;
    const uint MB_ICONWARNING = 0x30;
    const uint MB_ICONINFO = 0x40;
    const uint MB_SETFOREGROUND = 0x10000;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    static string root;
    static string logPath;
    static string portFile;

    [STAThread]
    static int Main(string[] args)
    {
        root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        portFile = Path.Combine(root, Path.Combine("app", "_cache"), "port.txt");
        logPath = Path.Combine(root, Path.Combine("_tools", "_launcher.log"));

        bool selfTest = false;
        for (int i = 0; i < args.Length; i++)
        {
            if (string.Equals(args[i], "--selftest", StringComparison.OrdinalIgnoreCase)) selfTest = true;
        }

        try { Directory.CreateDirectory(Path.Combine(root, "_tools")); }
        catch { }
        try { Directory.CreateDirectory(Path.Combine(root, "_replays")); }
        catch { }

        Log("============================================================");
        Log("启动器运行" + (selfTest ? "（自检）" : "") + "，目录 " + root);

        // 0) 已经有服务在跑，直接开页面
        string alive = AlivePort();
        if (alive != null)
        {
            Log("端口 " + alive + " 上已经有服务在跑，直接打开网页");
            if (!selfTest) OpenPage(alive);
            return 0;
        }

        // 1) 找 Python
        string pyExe;
        string pyPre;
        if (!FindPython(out pyExe, out pyPre))
        {
            Log("没找到 Python 运行时");
            if (!selfTest)
            {
                Box("内置的运行环境不见了（python\\python.exe）。\r\n\r\n"
                    + "可能是被删除或被安全软件隔离了，重新解压一份完整的程序包即可。\r\n"
                    + "也可以自己装 Python 3.10+（安装时勾选 Add python.exe to PATH）。", MB_ICONERROR);
            }
            return 2;
        }
        Log("python = [" + pyExe + "] 前缀 [" + pyPre + "]");

        string serverPy = Path.Combine(root, Path.Combine("app", "server.py"));
        if (!File.Exists(serverPy))
        {
            Log("找不到 " + serverPy);
            if (!selfTest) Box("程序文件不完整：找不到 app\\server.py。\r\n请重新解压一份完整的程序包。", MB_ICONERROR);
            return 2;
        }

        if (selfTest)
        {
            Log("自检通过");
            return 0;
        }

        // 2) 清掉上一次的端口记录
        try { if (File.Exists(portFile)) File.Delete(portFile); }
        catch { }
        try { Directory.CreateDirectory(Path.GetDirectoryName(portFile)); }
        catch { }

        // 3) 起服务（窗口隐藏，全程没有控制台）
        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = pyExe;
        psi.Arguments = pyPre + Quote(serverPy) + " --port " + StartPort + " --no-open";
        psi.WorkingDirectory = root;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.WindowStyle = ProcessWindowStyle.Hidden;
        Log("启动服务：" + psi.FileName + " " + psi.Arguments);
        try
        {
            Process.Start(psi);
        }
        catch (Exception e)
        {
            Log("起服务失败：" + e.Message);
            Box("没能启动服务进程：" + e.Message, MB_ICONERROR);
            return 2;
        }

        // 4) 等服务真的能用
        string port = WaitPortFile(30);          // 最多等 15 秒拿到端口
        if (port.Length == 0) port = StartPort.ToString();
        Log("port.txt = [" + port + "]");

        if (!WaitHealth(port, 30))               // 再等 15 秒，等 /api/health 有应答
        {
            Log("健康检查超时（端口 " + port + "）");
            // 探测不到不代表服务没起来，照样开网页，让人自己看
            OpenPage(port);
            Box("没能确认服务就绪（端口 " + port + "），但已经试着打开网页了。\r\n"
                + "如果页面打不开，手动跑一次看报错：\r\n" + pyExe + " app\\server.py",
                MB_ICONWARNING);
            return 1;
        }

        Log("健康检查通过，端口 " + port);
        OpenPage(port);
        Log("已调用打开网页，结束");
        return 0;
    }

    // ---------------------------------------------------------------- 工具

    static void Box(string text, uint icon)
    {
        MessageBoxW(IntPtr.Zero, text, AppName, icon | MB_SETFOREGROUND);
    }

    static void Log(string msg)
    {
        try
        {
            File.AppendAllText(logPath,
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + "  " + msg + "\r\n",
                new UTF8Encoding(false));
        }
        catch { }
    }

    static string Quote(string s)
    {
        return "\"" + s + "\"";
    }

    // 返回可直接塞进命令行参数的 Python 路径；pre 用于 "py -3" 这种解释器前缀
    static bool FindPython(out string exe, out string pre)
    {
        exe = "";
        pre = "";

        string[] local = { "python.exe", "pythonw.exe" };
        for (int i = 0; i < local.Length; i++)
        {
            string p = Path.Combine(root, Path.Combine("python", local[i]));
            if (File.Exists(p)) { exe = p; return true; }
        }

        string appData = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (!string.IsNullOrEmpty(appData))
        {
            string[] vers = { "Python314", "Python313", "Python312", "Python311", "Python310" };
            for (int i = 0; i < vers.Length; i++)
            {
                string p = Path.Combine(appData, Path.Combine("Programs", Path.Combine("Python", vers[i])));
                p = Path.Combine(p, "python.exe");
                if (File.Exists(p)) { exe = p; return true; }
            }
        }

        string py = FindInPath("py.exe");
        if (py.Length > 0) { exe = py; pre = "-3 "; return true; }

        string python = FindInPath("python.exe");
        if (python.Length > 0) { exe = python; return true; }

        return false;
    }

    static string FindInPath(string fileName)
    {
        string path = Environment.GetEnvironmentVariable("PATH");
        if (string.IsNullOrEmpty(path)) return "";
        string[] dirs = path.Split(';');
        for (int i = 0; i < dirs.Length; i++)
        {
            string d = dirs[i].Trim();
            if (d.Length == 0) continue;
            try
            {
                string p = Path.Combine(d, fileName);
                if (File.Exists(p)) return p;
            }
            catch { }
        }
        return "";
    }

    // 已经起着的服务在哪个端口上（优先看 port.txt，再看 8806）
    static string AlivePort()
    {
        string cached = ReadPortFile();
        if (cached.Length > 0 && Http200(cached)) return cached;
        string def = StartPort.ToString();
        if (Http200(def)) return def;
        return null;
    }

    static string ReadPortFile()
    {
        try
        {
            if (!File.Exists(portFile)) return "";
            string t = File.ReadAllText(portFile).Trim();
            int n;
            if (t.Length > 0 && t.Length < 6 && int.TryParse(t, out n) && n > 0 && n < 65536) return t;
        }
        catch { }
        return "";
    }

    static string WaitPortFile(int tries)
    {
        for (int i = 0; i < tries; i++)
        {
            string t = ReadPortFile();
            if (t.Length > 0) return t;
            Thread.Sleep(500);
        }
        return "";
    }

    static bool WaitHealth(string port, int tries)
    {
        for (int i = 0; i < tries; i++)
        {
            if (Http200(port)) return true;
            Thread.Sleep(500);
        }
        return false;
    }

    // 关键：Proxy = null，绕开系统代理（本机回环不该走代理）
    static bool Http200(string port)
    {
        try
        {
            HttpWebRequest req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port + "/api/health");
            req.Proxy = null;
            req.Timeout = 1000;
            req.ReadWriteTimeout = 1000;
            req.KeepAlive = false;
            req.UserAgent = AppName;
            using (HttpWebResponse res = (HttpWebResponse)req.GetResponse())
            {
                return res.StatusCode == HttpStatusCode.OK;
            }
        }
        catch { return false; }
    }

    static void OpenPage(string port)
    {
        string url = "http://127.0.0.1:" + port + "/";
        Log("打开 " + url);
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo(url);
            psi.UseShellExecute = true;
            Process.Start(psi);
        }
        catch (Exception e)
        {
            Log("打开浏览器失败：" + e.Message);
            Box("服务已经起来了，但打不开浏览器，请手动访问：\r\n" + url, MB_ICONINFO);
        }
    }
}
