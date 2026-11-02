"""IP 校验服务测试：IP 变动检测、自动重启 FTP、自动更新打印机注册信息。

全部用打桩（不打真打印机、不依赖真实网络变化）。
运行： python tests\\test_ipwatch.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BASE_DIR, os.path.join(BASE_DIR, "vendor")):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)


def _tk_available() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(_tk_available(), "没有可用桌面会话")
class IpWatchTest(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from app.main import App

        self.tmp = tempfile.TemporaryDirectory(prefix="scanlink-ipwatch-")
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.tmp.name, enable_tray=False)
        self.root.update()
        # 关掉可能自动起来的监听，逐个用例自己控制
        self.app._stop_ip_watch()

    def tearDown(self) -> None:
        try:
            self.app._stop_ftp()
        except Exception:
            pass
        try:
            self.app._closing = True
            self.root.destroy()
        except Exception:
            pass
        self.tmp.cleanup()

    # ------------------------------------------------------------------ 检测
    def test_tick_detects_change(self) -> None:
        from app import netinfo

        self.app._watch_printer_host = "192.168.1.100"
        self.app._last_known_ip = "192.168.1.10"
        original = netinfo.choose_local_ip
        try:
            netinfo.choose_local_ip = lambda *a, **k: "192.168.1.10"
            self.assertIsNone(self.app._ip_watch_tick(), "IP 没变时不应触发")

            netinfo.choose_local_ip = lambda *a, **k: "192.168.1.33"
            change = self.app._ip_watch_tick()
            self.assertIsNotNone(change, "IP 变了应触发")
            assert change is not None
            self.assertEqual(change[0], "192.168.1.10")
            self.assertEqual(change[1], "192.168.1.33")
            # 再检查一次不应重复触发
            self.assertIsNone(self.app._ip_watch_tick())
        finally:
            netinfo.choose_local_ip = original

    def test_tick_skips_when_no_printer(self) -> None:
        self.app._watch_printer_host = ""
        self.assertIsNone(self.app._ip_watch_tick())

    # ------------------------------------------------------------------ 变动处理
    def test_ip_change_restarts_ftp_and_reregisters(self) -> None:
        calls = {"ftp": 0, "register": 0}

        def fake_start_ftp(silent=False):
            calls["ftp"] += 1
            return True

        def fake_on_go():
            calls["register"] += 1

        self.app._start_ftp = fake_start_ftp
        self.app._on_go = fake_on_go
        self.app.cfg.printer_host = "192.168.1.100"
        self.app.cfg.last_name = "张三"
        self.app._last_auto_update_ts = 0.0
        self.app.ftp = object()  # 假装 FTP 在跑（running 属性由 _start_ftp 打桩兜住）
        self.app.ftp = type("F", (), {"running": True})()

        self.app._on_ip_changed("192.168.1.10", "192.168.1.33")

        self.assertEqual(calls["ftp"], 1, "IP 变了应重启 FTP")
        self.assertEqual(calls["register"], 1, "IP 变了应重新注册打印机")
        self.assertEqual(self.app.ip_var.get(), "192.168.1.33", "界面上的本机地址应更新")

    def test_ip_change_without_name_skips_register(self) -> None:
        calls = {"register": 0}
        self.app._on_go = lambda: calls.__setitem__("register", calls["register"] + 1)
        self.app._start_ftp = lambda silent=False: True
        self.app.cfg.printer_host = "192.168.1.100"
        self.app.cfg.last_name = ""
        self.app.name_var.set("")
        self.app.ftp = None

        self.app._on_ip_changed("192.168.1.10", "192.168.1.33")
        self.assertEqual(calls["register"], 0, "没有名字时不应注册")

    def test_cooldown_prevents_frequent_updates(self) -> None:
        import time

        calls = {"register": 0}
        self.app._on_go = lambda: calls.__setitem__("register", calls["register"] + 1)
        self.app._start_ftp = lambda silent=False: True
        self.app.cfg.printer_host = "192.168.1.100"
        self.app.cfg.last_name = "张三"
        self.app.ftp = None
        self.app._last_auto_update_ts = time.time()  # 刚更新过

        self.app._on_ip_changed("192.168.1.10", "192.168.1.33")
        self.assertEqual(calls["register"], 0, "冷却期内不应重复注册")

    def test_busy_skips_register(self) -> None:
        calls = {"register": 0}
        self.app._on_go = lambda: calls.__setitem__("register", calls["register"] + 1)
        self.app._start_ftp = lambda silent=False: True
        self.app.cfg.printer_host = "192.168.1.100"
        self.app.cfg.last_name = "张三"
        self.app.ftp = None
        self.app.busy = True
        self.app._last_auto_update_ts = 0.0

        self.app._on_ip_changed("192.168.1.10", "192.168.1.33")
        self.assertEqual(calls["register"], 0, "正在注册时不应并发触发")

    # ------------------------------------------------------------------ 开关
    def test_toggle_starts_and_stops_thread(self) -> None:
        self.app.cfg.printer_host = "192.168.1.100"
        self.app.watch_ip_var.set(True)
        self.app._on_watch_ip_toggle()
        self.assertIsNotNone(self.app._ip_watch_thread)
        self.assertTrue(self.app._ip_watch_thread.is_alive())
        self.assertIn("检查", self.app.watch_ip_hint.cget("text"))

        self.app.watch_ip_var.set(False)
        self.app._on_watch_ip_toggle()
        self.assertFalse(self.app.cfg.watch_ip)
        self.assertIn("已关闭", self.app.watch_ip_hint.cget("text"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
