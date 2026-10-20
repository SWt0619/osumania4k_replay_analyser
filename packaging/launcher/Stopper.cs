// osumania_replay_analyser —— 停止本地服务
//
// 启动器把服务放在后台（没有控制台窗口可按 Ctrl+C，也没有窗口可关），
// 所以用这个程序按命令行精确找出正在跑 app\server.py 的 python 进程并结束掉，
// 不会误伤电脑上别的 Python 程序。
//
// 逻辑与旧的 停止分析器.vbs 一致，只是换成 exe，不再走脚本宿主。

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Management;
using System.Runtime.InteropServices;

static class Stopper
{
    const string AppName = "osumania_replay_analyser";

    const uint MB_ICONERROR = 0x10;
    const uint MB_ICONINFO = 0x40;
    const uint MB_SETFOREGROUND = 0x10000;

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    [STAThread]
    static int Main(string[] args)
    {
        List<int> pids = new List<int>();

        // 只认「本目录下」的那个 app\server.py。
        // 以前只匹配命令行里有没有 "app\server.py"，同一台机器放两份副本时
        // （比如下了新版试一下）点哪个停止器都会把两个一起杀掉；别的 Python 项目
        // 入口恰好同名也会被误伤。
        //
        // 现在两条判据，命中任一条才算我们的：
        //   1) _cache\server.pid 里登记的那个 PID —— 服务自己写的，最精确，
        //      手动用相对路径 `python app\server.py` 启动也能认出来；
        //   2) 命令行里出现「本目录\app\server.py」的绝对路径。
        string sep = System.IO.Path.DirectorySeparatorChar.ToString();
        string ownDir = AppDomain.CurrentDomain.BaseDirectory
            .TrimEnd(System.IO.Path.DirectorySeparatorChar, System.IO.Path.AltDirectorySeparatorChar);
        string ownServer = (ownDir + sep + "app" + sep + "server.py").ToLowerInvariant();
        string ownServerAlt = ownServer.Replace("\\", "/");

        int pidFromFile = 0;
        try
        {
            // 源码形态缓存在 app\_cache，打包 exe 形态在 exe 旁边的 _cache，两处都看
            string[] pidPaths = new string[]
            {
                System.IO.Path.Combine(ownDir, System.IO.Path.Combine("app", "_cache")),
                System.IO.Path.Combine(ownDir, "_cache")
            };
            foreach (string dir in pidPaths)
            {
                string pidPath = System.IO.Path.Combine(dir, "server.pid");
                if (!System.IO.File.Exists(pidPath)) continue;
                if (int.TryParse(System.IO.File.ReadAllText(pidPath).Trim(), out pidFromFile)) break;
            }
        }
        catch { }

        try
        {
            ManagementObjectSearcher query = new ManagementObjectSearcher(
                "select ProcessId, CommandLine from Win32_Process");
            ManagementObjectCollection procs = query.Get();

            foreach (ManagementBaseObject mo in procs)
            {
                // CommandLine 对部分系统进程是 null，先挡一下
                object raw = mo["CommandLine"];
                string cmdline = raw == null ? "" : raw.ToString().ToLowerInvariant();
                if (cmdline.Length == 0) continue;

                int pid = 0;
                try { pid = Convert.ToInt32(mo["ProcessId"]); }
                catch { continue; }

                // 光看 server.py 会把别人的 Python 服务一起杀掉，所以先确认"是个 python 跑 server.py"
                bool runsServer = cmdline.IndexOf("python", StringComparison.Ordinal) >= 0
                    && (cmdline.IndexOf("app\\server.py", StringComparison.Ordinal) >= 0
                        || cmdline.IndexOf("app/server.py", StringComparison.Ordinal) >= 0);
                if (!runsServer) continue;

                bool isOurs = (pidFromFile != 0 && pid == pidFromFile)
                    || cmdline.IndexOf(ownServer, StringComparison.Ordinal) >= 0
                    || cmdline.IndexOf(ownServerAlt, StringComparison.Ordinal) >= 0;
                if (!isOurs) continue;

                pids.Add(pid);
            }

            procs.Dispose();
            query.Dispose();
        }
        catch (Exception e)
        {
            Box("没能枚举进程：" + e.Message, MB_ICONERROR);
            return 2;
        }

        // 先枚举完再动手：一边遍历 WMI 结果一边杀进程，容易在半路把枚举搞乱。
        //
        // 结束进程用 Process.Kill()（底层 TerminateProcess），不用 WMI 的 Terminate：
        // 实测在同一台机器上，ManagementObject.InvokeMethod("Terminate") 会抛
        // InvalidOperationException（"对象的当前状态使该操作无效"），CIM 通道却正常。
        // 既然 .NET 的 Process API 更直接也更稳，就别再绕 WMI。
        int killed = 0;
        int failed = 0;
        foreach (int pid in pids)
        {
            if (pid == Process.GetCurrentProcess().Id) continue;
            try
            {
                Process proc = Process.GetProcessById(pid);
                proc.Kill();
                proc.WaitForExit(3000);
                killed++;
            }
            catch (ArgumentException)
            {
                // 枚举到结束之间进程自己退了：算它停住了
                killed++;
            }
            catch
            {
                failed++;
            }
        }

        if (killed == 0 && failed == 0) Box("现在没有在跑的分析器服务。", MB_ICONINFO);
        else if (failed > 0) Box("已停止 " + killed + " 个服务进程，" + failed + " 个没能结束（权限不足时试试右键以管理员身份打开）。", MB_ICONINFO);
        else Box("已停止 " + killed + " 个服务进程。", MB_ICONINFO);

        return 0;
    }

    static void Box(string text, uint icon)
    {
        MessageBoxW(IntPtr.Zero, text, AppName, icon | MB_SETFOREGROUND);
    }
}
