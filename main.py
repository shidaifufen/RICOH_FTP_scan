"""扫描直连 · 一键注册 —— 程序入口。

直接双击运行（run.bat 也会调这个）。把 vendor 目录放进 sys.path，
所以不需要 pip 安装、不需要虚拟环境。
"""

from __future__ import annotations

import os
import sys

# 源码运行时的目录
_SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))

if getattr(sys, "frozen", False):
    # PyInstaller 打包后：exe 所在目录用来存 config.json / logs（可写、可持久化），
    # 而 vendor 依赖在解包出来的 _MEIPASS 里（只读）。
    APP_DIR = os.path.dirname(sys.executable)
    RESOURCE_DIR = getattr(sys, "_MEIPASS", _SOURCE_DIR)
else:
    APP_DIR = _SOURCE_DIR
    RESOURCE_DIR = _SOURCE_DIR


def _bootstrap_path() -> None:
    for path in (RESOURCE_DIR, os.path.join(RESOURCE_DIR, "vendor"), APP_DIR):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)


_MUTEX_HANDLE = None


def _single_instance() -> bool:
    """同一时间只允许一个实例：否则第二个实例会和托盘的旧实例抢 FTP 端口 21。"""
    global _MUTEX_HANDLE
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes
        from ctypes import wintypes

        # 必须用 use_last_error=True 的 WinDLL，否则 get_last_error() 永远是 0
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = kernel32.CreateMutexW(None, False, "Local\\ScanLinkSingleInstance")
        if not handle:
            return True
        _MUTEX_HANDLE = handle
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def _notify_already_running() -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "扫描直连 · 一键注册",
            "程序已经在运行了。\n\n"
            "请看屏幕右下角托盘图标：双击可打开窗口，右键可停止/退出。\n"
            "要重新启动，请先用托盘右键菜单「退出」。",
        )
        root.destroy()
    except Exception:
        pass


def main() -> int:
    _bootstrap_path()
    try:
        from app.main import main as run_gui
    except Exception as exc:  # 依赖缺失时给出人能看懂的信息
        import traceback

        traceback.print_exc()
        try:
            import tkinter.messagebox as mb
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            mb.showerror(
                "扫描直连 · 一键注册",
                "程序启动失败，缺少依赖或文件不完整。\n\n"
                f"{exc}\n\n"
                "请把整个「扫描直连」文件夹一起拷过来（里面要有 vendor 目录），\n"
                "或运行 setup.bat 重新安装依赖。",
            )
        except Exception:
            pass
        return 1

    if "--cli-register" not in sys.argv[1:] and not _single_instance():
        _notify_already_running()
        return 0

    if "--cli-register" in sys.argv[1:]:
        from app.main import run_cli

        return run_cli(APP_DIR, sys.argv[1:])

    run_gui(APP_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
