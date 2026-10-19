// osumania_replay_analyser —— 停止本地服务
//
// 启动器把服务放在后台（没有控制台窗口可按 Ctrl+C，也没有窗口可关），
// 所以用这个程序按命令行精确找出正在跑 app\server.py 的 python 进程并结束掉，
// 不会误伤电脑上别的 Python 程序。
//
// 逻辑与旧的 停止分析器.vbs 一致，只是换成 exe，不再走脚本宿主。

using System;
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
        int killed = 0;
        int failed = 0;

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

                // 只认本项目的 app\server.py —— 光看 server.py 会把别人的 Python 服务一起杀掉
                bool isOurs = cmdline.IndexOf("app\\server.py", StringComparison.Ordinal) >= 0
                           || cmdline.IndexOf("app/server.py", StringComparison.Ordinal) >= 0;
                if (!isOurs) continue;
                if (cmdline.IndexOf("python", StringComparison.Ordinal) < 0) continue;

                // InvokeMethod 在 ManagementObject 上，基类没有
                ManagementObject proc = mo as ManagementObject;
                if (proc == null) continue;

                try
                {
                    proc.InvokeMethod("Terminate", null);
                    killed++;
                }
                catch
                {
                    failed++;
                }
            }

            procs.Dispose();
            query.Dispose();
        }
        catch (Exception e)
        {
            Box("没能枚举进程：" + e.Message, MB_ICONERROR);
            return 2;
        }

        if (killed == 0 && failed == 0) Box("现在没有在跑的分析器服务。", MB_ICONINFO);
        else if (failed > 0) Box("已停止 " + killed + " 个服务进程，" + failed + " 个没能结束（可能在别的用户下运行，试试以管理员身份打开）。", MB_ICONINFO);
        else Box("已停止 " + killed + " 个服务进程。", MB_ICONINFO);

        return 0;
    }

    static void Box(string text, uint icon)
    {
        MessageBoxW(IntPtr.Zero, text, AppName, icon | MB_SETFOREGROUND);
    }
}
