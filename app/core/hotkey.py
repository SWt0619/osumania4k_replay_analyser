"""全局热键：按一下就把"刚导出的那份回放"接进来。

只做一件事——在 Windows 上注册一个组合键，按下时回调。用 ctypes 直接调 user32，
不引第三方库，也不装键盘钩子：`RegisterHotKey` 是系统提供的注册机制，
只接管你指定的那一个组合，别的键（包括游戏自己的 F2）照常送到前台窗口。

为什么用 PeekMessage 轮询而不是 GetMessage 阻塞
------------------------------------------------
阻塞式消息循环没法从外部叫停（要另外 `PostThreadMessage(WM_QUIT)` 并处理线程 id）。
这里 50 毫秒看一次队列，空转开销可以忽略，而 `stop()` 只要置一个事件就能干净退出，
对"服务随时可能被关掉"的本地工具来说更省心。
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
HOTKEY_ID = 0x4D51            # 随便取的进程内 id
POLL_INTERVAL = 0.05

_MOD_NAMES = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN,
}

_VK_NAMES = {"F%d" % i: 0x6F + i for i in range(1, 13)}
_VK_NAMES.update({chr(c): c for c in range(ord("A"), ord("Z") + 1)})
_VK_NAMES.update({str(d): 0x30 + d for d in range(10)})

_state = {
    "ok": False,
    "error": "",
    "combo": "Shift+F2",
    "since": 0.0,
    "presses": 0,
}
_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None


def parse_combo(combo: str) -> tuple[int, int, str]:
    """`"Ctrl+Shift+F2"` → (修饰键, 虚拟键码, 规范化写法)。

    认不出来时抛 ValueError：宁可启动时明确报错，也不要静默注册一个别的键。
    """
    mods = 0
    vk = 0
    parts = [p.strip() for p in str(combo or "").split("+") if p.strip()]
    if not parts:
        raise ValueError("快捷键是空的")
    for part in parts:
        key = part.lower()
        if key in _MOD_NAMES:
            mods |= _MOD_NAMES[key]
            continue
        name = part.upper()
        if name in _VK_NAMES:
            vk = _VK_NAMES[name]
            continue
        raise ValueError("认不出这个按键：%s" % part)
    if not vk:
        raise ValueError("快捷键里缺少主键（比如 F2）")
    if not mods:
        # 单键会被游戏和其他程序大量占用，强制要求带修饰键
        raise ValueError("快捷键至少要带一个 Ctrl / Alt / Shift")
    pretty = "+".join(
        [n for n, bit in (("Ctrl", MOD_CONTROL), ("Alt", MOD_ALT),
                          ("Shift", MOD_SHIFT), ("Win", MOD_WIN)) if mods & bit]
        + [next(k for k, v in _VK_NAMES.items() if v == vk)]
    )
    return mods, vk, pretty


def _loop(mods: int, vk: int, callback) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
        with _lock:
            _state["ok"] = False
            _state["error"] = "注册失败（错误码 %s），这个组合可能被别的程序占用了" % ctypes.get_last_error()
        return
    with _lock:
        _state["ok"] = True
        _state["error"] = ""
        _state["since"] = time.time()

    msg = wintypes.MSG()
    try:
        while not _stop.is_set():
            if user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    with _lock:
                        _state["presses"] += 1
                    # 回调放到独立线程：分析要几百毫秒，不能卡住消息循环
                    threading.Thread(target=callback, daemon=True).start()
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            else:
                time.sleep(POLL_INTERVAL)
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)
        with _lock:
            _state["ok"] = False


def start(callback, combo: str = "Shift+F2") -> tuple[bool, str]:
    """注册热键并起后台线程。返回（是否成功, 错误说明）。"""
    global _thread
    if sys.platform != "win32":
        return False, "全局热键目前只在 Windows 上实现"
    try:
        mods, vk, pretty = parse_combo(combo)
    except ValueError as e:
        with _lock:
            _state.update(ok=False, error=str(e), combo=str(combo))
        return False, str(e)
    with _lock:
        _state["combo"] = pretty
    if _thread is not None and _thread.is_alive():
        return True, ""
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(mods, vk, callback),
                               name="quick-hotkey", daemon=True)
    _thread.start()
    # 等一下注册结果：注册失败时上层要能立刻提示用户
    for _ in range(20):
        time.sleep(0.05)
        with _lock:
            if _state["ok"]:
                return True, ""
            if _state["error"]:
                return False, _state["error"]
    return False, "热键线程没有就绪"


def stop(clear_combo: bool = False) -> None:
    """注销热键并结束线程（服务退出时调用）。

    clear_combo=True 表示用户主动关掉了快捷导入：连"上次用的组合"一起忘掉，
    否则设置页会把刚清空的输入框又填回旧值。
    """
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=1.0)
        _thread = None
    if clear_combo:
        with _lock:
            _state["combo"] = ""


def status() -> dict:
    with _lock:
        return dict(_state)
