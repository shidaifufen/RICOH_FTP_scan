"""在受限令牌（runas /trustlevel，等价于普通用户双击）下探测：

1. Shell_NotifyIcon 是否被拒（复现用户看到的错误码 5）
2. 桌面\\扫描 是否可写（复现用户看到的 WinError 5）
3. 消息专用窗口 vs 普通隐藏顶层窗口，哪种能加托盘图标

结果写到环境变量 TRAY_RESULT 指定的文件里（runas 会开新控制台，看不到 stdout）。
运行方式：
    runas /trustlevel:0x20000 "python.exe tests\\probe_lowil.py"
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BASE_DIR, os.path.join(BASE_DIR, "vendor")):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

RESULTS: list[str] = []


def note(msg: str) -> None:
    RESULTS.append(str(msg))


def flush() -> None:
    path = os.environ.get("TRAY_RESULT") or os.path.join(BASE_DIR, "logs", "lowil-result.txt")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(RESULTS))
    except OSError:
        pass


def whoami_and_privs() -> None:
    try:
        import subprocess

        out = subprocess.run(["whoami", "/priv"], capture_output=True, text=True, timeout=10)
        note("[whoami] " + (out.stdout or "").strip().splitlines()[0] if out.stdout else "[whoami] ?")
    except Exception as exc:
        note(f"[whoami] 失败 {exc}")
    try:
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        admin = False
    note(f"[管理员] IsUserAnAdmin={admin}")


def probe_desktop_write() -> None:
    from app.config import desktop_dir, default_scan_dir

    target = default_scan_dir()
    note(f"[桌面] desktop_dir={desktop_dir()}")
    note(f"[桌面] 目标={target}")
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, "_scanlink_write_test.txt")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        note("[桌面] 可写 ✅")
    except OSError as exc:
        note(f"[桌面] 不可写 ❌ {exc}")
    for fallback in (os.path.join(os.path.expanduser("~"), "Documents", "扫描"),
                     os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "扫描直连", "扫描")):
        try:
            os.makedirs(fallback, exist_ok=True)
            note(f"[备选] 可写 ✅ {fallback}")
        except OSError as exc:
            note(f"[备选] 不可写 ❌ {fallback} -> {exc}")


def try_notify_icon(use_message_only: bool) -> None:
    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
    ]
    user32.LoadIconW.restype = wt.HANDLE
    user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
    user32.RegisterClassExW.restype = wt.WORD
    user32.LoadImageW.restype = wt.HANDLE
    user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
    kernel32.GetModuleHandleW.restype = wt.HINSTANCE

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
            ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
            ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
            ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
            ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON),
        ]

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
            ("hbrBackground", wt.HANDLE), ("lpszMenuName", wt.LPCWSTR),
            ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
        ]

    kind = "消息专用窗口" if use_message_only else "隐藏顶层窗口"
    class_name = f"ProbeTray{int(use_message_only)}"
    proc = WNDPROC(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = proc
    wc.hInstance = hinst
    wc.lpszClassName = class_name
    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if not atom:
        note(f"[托盘-{kind}] RegisterClassEx 失败 err={ctypes.get_last_error()}")
        return
    parent = ctypes.c_void_p(-3 & 0xFFFFFFFFFFFFFFFF) if use_message_only else None
    style = 0 if use_message_only else 0x00000000  # 顶层窗口，不显示
    hwnd = user32.CreateWindowExW(0, class_name, "probe", style, 0, 0, 0, 0, parent, None, hinst, None)
    if not hwnd:
        note(f"[托盘-{kind}] CreateWindow 失败 err={ctypes.get_last_error()}")
        return
    note(f"[托盘-{kind}] 窗口句柄=0x{int(hwnd or 0):X}")

    icon_path = os.path.join(BASE_DIR, "app", "scanlink.ico")
    hicon = 0
    if os.path.isfile(icon_path):
        hicon = int(user32.LoadImageW(None, icon_path, 1, 32, 32, 0x0010) or 0)
    note(f"[托盘-{kind}] 图标句柄={hicon} (文件存在={os.path.isfile(icon_path)})")
    if not hicon:
        hicon = int(user32.LoadIconW(None, 32512) or 0)

    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = 0x1 | 0x2 | 0x4
    nid.uCallbackMessage = 0x0400 + 1
    nid.hIcon = hicon
    nid.szTip = "探针"
    note(f"[托盘-{kind}] cbSize={nid.cbSize}")
    ok = shell32.Shell_NotifyIconW(0, ctypes.byref(nid))
    err = ctypes.get_last_error()
    note(f"[托盘-{kind}] NIM_ADD={'成功' if ok else '失败'} err={err}")
    if ok:
        time.sleep(0.5)
        shell32.Shell_NotifyIconW(2, ctypes.byref(nid))
    user32.DestroyWindow(hwnd)
    user32.UnregisterClassW(class_name, hinst)


def main() -> int:
    whoami_and_privs()
    probe_desktop_write()
    try:
        try_notify_icon(True)
        try_notify_icon(False)
    except Exception as exc:
        note(f"[托盘] 异常 {exc!r}")
    flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
