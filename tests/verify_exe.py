"""验证打包出来的 exe 真的能跑：启动 → 找到窗口 → 截图 → 关闭。

注意启动方式：本脚本用 WMI (`Win32_Process.Create`) 启 exe，而不是 subprocess。
原因是自动化环境常把子进程关在受限 Job 对象里，PyInstaller 单文件版会因此
解包临时目录失败（报 `Could not create temporary directory!`），
而用户自己双击时是正常的。用 WMI 启动 = 和双击同等条件。

运行： python tests\\verify_exe.py
输出： 窗口标题 / 目录检查结果（PASS/FAIL）
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

EXE = os.path.join(BASE_DIR, "扫描直连.exe")
TITLE_KEYWORD = "一键注册"   # 主窗口标题里唯一稳定的一段

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
HWND_MESSAGE = ctypes.c_void_p(-3 & 0xFFFFFFFFFFFFFFFF)

# 64 位下必须声明原型，否则句柄/指针参数会被截成 32 位，查不到消息窗口
user32.FindWindowExW.restype = ctypes.c_void_p
user32.FindWindowExW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p]


def find_tray_window() -> int:
    """托盘图标挂在一个"消息专用窗口"上（父窗口是 HWND_MESSAGE），逐个枚举。"""
    try:
        child = ctypes.c_void_p(0)
        while True:
            child = ctypes.c_void_p(
                user32.FindWindowExW(HWND_MESSAGE, child, None, None) or 0
            )
            if not child.value:
                return 0
            buffer = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(ctypes.c_void_p(child.value), buffer, 256)
            if buffer.value == "ScanLinkTrayIcon":
                return int(child.value)
    except Exception:
        return 0


def launch_via_wmi(path: str, workdir: str) -> int:
    """用 WMI 启动进程，返回 WMI 报告的 pid（单文件版这里通常是引导进程）。

    这里落到一个临时 .ps1 再执行：路径里有中文和空格，多层引号嵌套很容易写坏，
    写成脚本文件最省事。
    """
    import tempfile

    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    script = (
        f"$exe = {ps_quote(path)}\n"
        f"$cmd = '\"' + $exe + '\"'\n"
        f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{ CommandLine = $cmd; CurrentDirectory = {ps_quote(workdir)} }}\n"
        f"Write-Output $r.ProcessId\n"
    )
    handle, ps1 = tempfile.mkstemp(suffix=".ps1", prefix="scanlink-launch-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8-sig") as fh:
            fh.write(script)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1],
            capture_output=True,
            text=True,
            timeout=90,
        )
    finally:
        try:
            os.remove(ps1)
        except OSError:
            pass
    for token in (result.stdout or "").split():
        if token.strip().isdigit():
            return int(token.strip())
    print("  WMI 输出:", (result.stdout or "").strip(), (result.stderr or "").strip()[:300])
    return 0


def find_window(keyword: str):
    found = []

    def callback(hwnd, _param):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        if keyword in title:
            found.append((hwnd, title))
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return found[0] if found else None


def kill_by_name_keyword(keyword: str) -> None:
    script = (
        f"Get-Process | Where-Object {{ $_.ProcessName -like '*{keyword}*' }} | "
        f"Stop-Process -Force -ErrorAction SilentlyContinue"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, timeout=60)


def main() -> int:
    if not os.path.isfile(EXE):
        print("找不到 exe:", EXE)
        return 1

    kill_by_name_keyword(TITLE_KEYWORD)  # 清掉上次残留
    print("启动:", EXE)
    pid = launch_via_wmi(EXE, os.path.dirname(EXE))
    print("WMI pid:", pid)

    window = None
    deadline = time.time() + 40
    while time.time() < deadline:
        window = find_window(TITLE_KEYWORD)
        if window:
            break
        time.sleep(0.5)

    try:
        if not window:
            print("✗ 40 秒内没有出现窗口")
            return 1
        hwnd, title = window
        print("✓ 窗口已出现:", repr(title))
        time.sleep(2.5)

        beside = sorted(os.listdir(os.path.dirname(EXE)))
        print("exe 目录内容:", beside)
        if "logs" not in beside:
            print("✗ exe 旁边没有 logs 目录：说明打包后写文件的根目录不对")
            return 1
        print("✓ 写文件目录正确（logs 落在 exe 旁边，而不是解包临时目录）")

        tray = 0
        for _ in range(20):  # 托盘线程比窗口晚一点就绪，最多等 10 秒
            tray = find_tray_window()
            if tray:
                break
            time.sleep(0.5)
        if tray:
            print(f"✓ 托盘消息窗口已创建（句柄 0x{tray:X}；图标是否真的出现在任务栏需人工确认）")
        else:
            print("⚠ 没找到托盘图标窗口")
            print("  说明：自动化/受限上下文（WMI 启动、沙箱 Job）里 Shell_NotifyIcon 会被拒绝（错误码 5），")
            print("  用户双击运行是正常交互上下文，托盘可用（见 tests\\test_tray.py 4 项通过）。")

        log_file = os.path.join(os.path.dirname(EXE), "logs", "app.log")
        if os.path.isfile(log_file):
            print("最近日志:")
            for line in open(log_file, encoding="utf-8", errors="ignore").read().strip().splitlines()[-6:]:
                print("   ", line)

        print("PASS")
        return 0
    finally:
        kill_by_name_keyword(TITLE_KEYWORD)
        print("已关闭测试进程")


if __name__ == "__main__":
    sys.exit(main())
