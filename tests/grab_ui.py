"""给界面截图（写文档用）。

这台机器上 ImageGrab 抓屏失败（非交互会话），所以改用 Win32 `PrintWindow`：
直接让窗口把自己画到内存位图上，不依赖屏幕可访问性。

运行： python tests\\grab_ui.py
输出： docs/界面.png
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BASE_DIR, os.path.join(BASE_DIR, "vendor")):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

PW_RENDERFULLCONTENT = 0x00000002
GA_ROOT = 2
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def grab_window(hwnd: int):
    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise RuntimeError("窗口尺寸无效")

    window_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    gdi32.SelectObject(mem_dc, bitmap)
    user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # 负数 = top-down
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0

    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS)

    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, window_dc)

    return Image.frombuffer("RGBA", (width, height), buffer, "raw", "BGRA", 0, 1).convert("RGB")


def main() -> int:
    import tkinter as tk

    from app.main import App

    os.makedirs(os.path.join(BASE_DIR, "docs"), exist_ok=True)
    root = tk.Tk()
    root.geometry("900x780+60+40")
    app = App(root, BASE_DIR, enable_tray=False)

    # 填一份示意数据，让截图看着像真实使用场景（全部为占位地址，不含真实内网信息）
    app.printer_var.set("192.168.1.100")
    app.admin_pwd_var.set("")
    app.name_var.set("张三")
    app.dir_var.set(r"D:\扫描件\张三")
    app.merge_by_ip_var.set(True)
    app.ip_var.set("192.168.1.10")

    # 真起一个 FTP（非 21 端口，避免和本机已有服务冲突），
    # 让状态标签真实显示「● 已启动」绿色，同时把对外地址改成占位值
    import tempfile

    demo_dir = os.path.join(tempfile.mkdtemp(prefix="scanlink-shot-"), "扫描件")
    app.port_var.set("2322")
    app.dir_var.set(demo_dir)
    app._start_ftp(silent=True)
    if app.ftp is not None:
        app.ftp.masquerade_ip = "192.168.1.10"
        app._set_status(app.ftp.status_line(), "#1a7f37")
    app.ip_var.set("192.168.1.10")
    app.dir_var.set(r"D:\扫描件\张三")

    app.log("本机地址（给打印机用）：192.168.1.10")
    app.log("FTP 服务已启动，扫描件会保存到：D:\\扫描件\\张三")
    app.log("防火墙：放通 TCP 21 成功 · TCP 30000-30099 成功")
    app.log("===== 开始注册「张三」 =====")
    app.log("[打印机] ✓ 连接打印机后台：已连接：MP C4504（HTTP 200）")
    app.log("[打印机] ✓ 管理员登录：admin")
    app.log("[打印机] ✓ 进入通讯簿")
    app.log("[打印机] ✓ 分配注册编号：00008")
    app.log("[打印机] ✓ 填写文件夹目的地：FTP://192.168.1.10:21/，用户 anonymous")
    app.log("[打印机] ✓ 校验注册结果：地址列表已出现「张三」")

    deadline = time.time() + 2.5
    while time.time() < deadline:
        root.update()
        time.sleep(0.05)
    root.update_idletasks()

    hwnd = ctypes.windll.user32.GetAncestor(root.winfo_id(), GA_ROOT)
    image = grab_window(hwnd)
    out = os.path.join(BASE_DIR, "docs", "界面.png")
    image.save(out)
    print("saved:", out, image.size)

    app._closing = True
    try:
        app._stop_ftp()
    except Exception:
        pass
    root.destroy()
    return 0


if __name__ == "__main__":
    sys.exit(main())
