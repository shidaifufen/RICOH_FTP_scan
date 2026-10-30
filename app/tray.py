"""Windows 通知区域（托盘）图标：纯 Win32 实现，不引第三方依赖。

为什么要它：FTP 服务在后台跑着的时候，用户需要一个"看得见"的入口——
右下角有图标，右键能快速启动/停止 FTP、看状态、打开保存目录、退出。
否则窗口一关，服务还在跑但没人知道。

实现要点：
* 用一个"消息专用窗口"（HWND_MESSAGE）接收托盘回调；
* 菜单项由调用方通过 `menu_provider` 动态提供，所以能随 FTP 状态变化；
* 所有 UI 操作必须回到 Tk 线程 —— 这里只把动作放进队列，由主程序取走执行；
* 资源管理：Shell_NotifyIcon 成对 NIM_ADD / NIM_DELETE，窗口和菜单及时释放；
* Explorer 重启会丢掉托盘图标，所以监听 "TaskbarCreated" 消息重新加回来。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time
from typing import Callable, List, Optional, Sequence, Tuple

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_USER = 0x0400
WM_TRAY = WM_USER + 1
WM_REFRESH = WM_USER + 2
WM_STOP = WM_USER + 3
WM_NULL = 0x0000

WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 0x1, 0x2, 0x4

MF_STRING, MF_SEPARATOR, MF_GRAYED = 0x0000, 0x0800, 0x0001
TPM_RETURNCMD, TPM_NONOTIFY, TPM_RIGHTBUTTON = 0x0100, 0x0080, 0x0002
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x0010, 0x0040
IDI_APPLICATION = 32512
HWND_MESSAGE = -3

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

MenuProvider = Callable[[], List[Optional[Tuple[str, str, bool]]]]
MenuDispatch = Callable[[str], None]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.DWORD),
        ("hWnd", wt.HWND),
        ("uID", wt.UINT),
        ("uFlags", wt.UINT),
        ("uCallbackMessage", wt.UINT),
        ("hIcon", wt.HICON),
        ("szTip", wt.WCHAR * 128),
        ("dwState", wt.DWORD),
        ("dwStateMask", wt.DWORD),
        ("szInfo", wt.WCHAR * 256),
        ("uVersion", wt.UINT),
        ("szInfoTitle", wt.WCHAR * 64),
        ("dwInfoFlags", wt.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wt.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HANDLE),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON),
    ]


# ---------------------------------------------------------------- Win32 函数原型
# 64 位下句柄是 8 字节，必须显式声明 restype，否则返回值会被截成 32 位。
user32.RegisterClassExW.restype = wt.WORD
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadIconW.restype = wt.HANDLE
user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, wt.UINT, wt.LPCWSTR]
user32.TrackPopupMenu.restype = wt.UINT
user32.TrackPopupMenu.argtypes = [
    wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID,
]
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.GetMessageW.restype = ctypes.c_int
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
shell32.Shell_NotifyIconW.restype = wt.BOOL
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]


def _icon_path() -> Optional[str]:
    """找托盘图标文件。源码运行、单文件打包、文件夹打包三种位置都覆盖。"""
    candidates = []
    here = os.path.dirname(os.path.abspath(__file__))
    candidates += [os.path.join(here, "scanlink.ico"), os.path.join(here, "app", "scanlink.ico")]
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        candidates += [os.path.join(bundle, "app", "scanlink.ico"), os.path.join(bundle, "scanlink.ico")]
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app", "scanlink.ico"))
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


class TrayIcon:
    """一个托盘图标 + 右键菜单。

    menu_provider() 返回 [(id, 显示文字, 是否可点), None(分隔线), ...]，
    每次右键都重新取一次，所以菜单能反映 FTP 的实时状态。
    """

    CLASS_NAME = "ScanLinkTrayIcon"

    def __init__(
        self,
        tooltip: str = "扫描直连",
        menu_provider: Optional[MenuProvider] = None,
        on_menu: Optional[MenuDispatch] = None,
        on_show: Optional[Callable[[], None]] = None,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.tooltip = (tooltip or "扫描直连")[:120]
        self.menu_provider = menu_provider or (lambda: [])
        self.on_menu = on_menu or (lambda _id: None)
        self.on_show = on_show or (lambda: None)
        self.log = log or (lambda _msg: None)

        self._hwnd: Optional[int] = None
        self._nid: Optional[NOTIFYICONDATAW] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._wndproc = None  # 必须保引用，否则回调会被 GC 掉
        self._class_atom = 0
        self._taskbar_created_msg = 0
        self._running = False
        self._error = ""

    # ------------------------------------------------------------------ 对外
    @property
    def available(self) -> bool:
        return self._running

    @property
    def last_error(self) -> str:
        return self._error

    def start(self, wait: float = 5.0) -> bool:
        if self._running:
            return True
        if not hasattr(ctypes, "windll"):
            self._error = "非 Windows 环境，托盘不可用"
            return False
        self._thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=wait)
        return self._running

    def set_tooltip(self, text: str) -> None:
        self.tooltip = (text or "扫描直连")[:120]
        self.refresh()

    def refresh(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_REFRESH, 0, 0)

    def stop(self) -> None:
        if self._hwnd:
            user32.PostMessageW(self._hwnd, WM_STOP, 0, 0)
            if self._thread is not None:
                self._thread.join(timeout=3)
        self._running = False
        self._hwnd = None

    # ------------------------------------------------------------------ 线程内实现
    def _run(self) -> None:
        try:
            self._create_window()
            self._add_icon()
            self._running = True
            self._ready.set()
            self.log(f"[托盘] 图标已就绪（{self.tooltip}）")
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # pragma: no cover - 托盘失败不能拖垮主程序
            self._error = str(exc)
            self.log(f"[托盘] 初始化失败：{exc}")
        finally:
            self._ready.set()
            self._running = False
            try:
                self._delete_icon()
                if self._hwnd:
                    user32.DestroyWindow(self._hwnd)
                if self._class_atom:
                    user32.UnregisterClassW(self.CLASS_NAME, kernel32.GetModuleHandleW(None))
            except Exception:
                pass

    def _create_window(self) -> None:
        hinstance = kernel32.GetModuleHandleW(None)
        self._wndproc = WNDPROC(self._on_message)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc
        wc.cbClsExtra = 0
        wc.cbWndExtra = 0
        wc.hInstance = hinstance
        wc.hIcon = self._load_icon()
        wc.hCursor = 0
        wc.hbrBackground = 0
        wc.lpszMenuName = None
        wc.lpszClassName = self.CLASS_NAME
        wc.hIconSm = wc.hIcon
        self._class_atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not self._class_atom:
            raise RuntimeError(
                f"RegisterClassExW 失败（错误码 {ctypes.get_last_error()}）"
            )

        self._hwnd = user32.CreateWindowExW(
            0, self.CLASS_NAME, "扫描直连托盘", 0,
            0, 0, 0, 0, HWND_MESSAGE, None, hinstance, None,
        )
        if not self._hwnd:
            raise RuntimeError(f"CreateWindowExW 失败（错误码 {ctypes.get_last_error()}）")
        self._taskbar_created_msg = user32.RegisterWindowMessageW("TaskbarCreated")

    def _load_icon(self) -> int:
        path = _icon_path()
        if path:
            handle = user32.LoadImageW(None, path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            if handle:
                return int(handle)
            self.log(f"[托盘] 图标文件加载失败（{path}，错误码 {ctypes.get_last_error()}），改用系统图标")
        stock = user32.LoadIconW(None, IDI_APPLICATION)
        return int(stock or 0)

    def _add_icon(self) -> None:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._load_icon()
        nid.szTip = self.tooltip
        self._nid = nid
        # 任务栏刚启动/正忙时 NIM_ADD 可能短暂失败，多试几次（带错误码上报）
        last_error = 0
        for attempt in range(5):
            if shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
                return
            last_error = ctypes.get_last_error()
            if attempt == 0:
                self.log(f"[托盘] Shell_NotifyIcon 第 1 次失败（错误码 {last_error}），重试中…")
            time.sleep(0.4)
        raise RuntimeError(
            f"Shell_NotifyIconW(NIM_ADD) 失败（错误码 {last_error}，"
            f"图标句柄 {int(nid.hIcon or 0)}）"
        )

    def _update_icon(self) -> None:
        if self._nid is None:
            return
        self._nid.uFlags = NIF_ICON | NIF_TIP
        self._nid.hIcon = self._load_icon()
        self._nid.szTip = self.tooltip
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def _delete_icon(self) -> None:
        if self._nid is not None:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self._nid = None

    def _on_message(self, hwnd, msg, wparam, lparam):  # noqa: ANN001
        try:
            if msg == self._taskbar_created_msg and self._taskbar_created_msg:
                # Explorer 重启后托盘会清空，重新添加
                self._add_icon()
                return 0
            if msg == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (WM_RBUTTONUP, WM_CONTEXTMENU):
                    self._show_menu(hwnd)
                elif event in (WM_LBUTTONDBLCLK, WM_LBUTTONUP):
                    self.on_show()
                return 0
            if msg == WM_REFRESH:
                self._update_icon()
                return 0
            if msg == WM_STOP:
                self._delete_icon()
                user32.DestroyWindow(hwnd)
                user32.PostQuitMessage(0)
                return 0
        except Exception as exc:  # pragma: no cover
            self.log(f"[托盘] 消息处理异常：{exc}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _show_menu(self, hwnd) -> None:
        items = self.menu_provider() or []
        menu = user32.CreatePopupMenu()
        if not menu:
            return
        ids: List[str] = []
        try:
            for item in items:
                if item is None:
                    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    continue
                item_id, label, enabled = item
                flags = MF_STRING | (0 if enabled else MF_GRAYED)
                user32.AppendMenuW(menu, flags, len(ids) + 1, label)
                ids.append(item_id)

            point = wt.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            chosen = user32.TrackPopupMenu(
                menu,
                TPM_RETURNCMD | TPM_NONOTIFY | TPM_RIGHTBUTTON,
                point.x, point.y, 0, hwnd, None,
            )
            user32.PostMessageW(hwnd, WM_NULL, 0, 0)
            if chosen and 0 < chosen <= len(ids):
                self.on_menu(ids[chosen - 1])
        finally:
            user32.DestroyMenu(menu)
