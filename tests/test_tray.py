"""托盘图标测试：图标能否注册、菜单是否随 FTP 状态变化、动作能否触发。

注意：托盘需要真正的桌面会话（explorer）。没有的话本测试会跳过。
运行： python tests\\test_tray.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
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


class TrayIconTest(unittest.TestCase):
    def test_tray_registers_and_stops(self) -> None:
        from app.tray import TrayIcon

        logs = []
        tray = TrayIcon(
            tooltip="扫描直连 · 测试",
            menu_provider=lambda: [("show", "打开主界面", True), None, ("quit", "退出", True)],
            on_menu=lambda action: logs.append(action),
            on_show=lambda: logs.append("show"),
            log=logs.append,
        )
        try:
            self.assertTrue(tray.start(), f"托盘启动失败：{tray.last_error}")
            self.assertTrue(tray.available)
            tray.set_tooltip("扫描直连 · FTP 运行中 10.0.0.2:21")
            time.sleep(0.3)
            self.assertIn("FTP 运行中", tray.tooltip)
        finally:
            tray.stop()
        self.assertFalse(tray.available)


@unittest.skipUnless(_tk_available(), "没有可用桌面会话")
class AppTrayTest(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from app.main import App

        self.tmp = tempfile.TemporaryDirectory(prefix="scanlink-tray-")
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.tmp.name, enable_tray=True)
        self.root.update()

    def tearDown(self) -> None:
        try:
            self.app._stop_ftp()
        except Exception:
            pass
        try:
            if self.app.tray is not None:
                self.app.tray.stop()
        except Exception:
            pass
        self.root.destroy()
        self.tmp.cleanup()

    def test_menu_reflects_ftp_state(self) -> None:
        self.assertIsNotNone(self.app.tray, "托盘没有创建成功")
        items = self.app._tray_menu_items()
        ids = [item[0] for item in items if item]
        self.assertIn("show", ids)
        self.assertIn("quit", ids)
        self.assertIn("start", ids)
        # 未启动时「停止」不可点、「启动」可点
        by_id = {item[0]: item for item in items if item}
        self.assertTrue(by_id["start"][2])
        self.assertFalse(by_id["stop"][2])

        # 启动 FTP（换端口避免冲突）后菜单应反过来
        self.app.port_var.set("2399")
        self.app.dir_var.set(os.path.join(self.tmp.name, "扫描件"))
        self.assertTrue(self.app._start_ftp(silent=True))
        items = self.app._tray_menu_items()
        by_id = {item[0]: item for item in items if item}
        self.assertFalse(by_id["start"][2])
        self.assertTrue(by_id["stop"][2])
        self.assertTrue(self.app.tray.tooltip.startswith("扫描直连 · FTP 运行中"))

    def test_tooltip_updates_on_stop(self) -> None:
        self.app.port_var.set("2398")
        self.app.dir_var.set(os.path.join(self.tmp.name, "扫描件2"))
        self.app._start_ftp(silent=True)
        self.assertIn("运行中", self.app.tray.tooltip)
        self.app._stop_ftp()
        self.assertIn("未启动", self.app.tray.tooltip)

    def test_close_hides_to_tray(self) -> None:
        # 点 X 应收进托盘而不是退出
        self.app._on_close()
        self.assertFalse(self.app._closing, "点 X 不应该真的退出")
        self.assertEqual(self.root.state(), "withdrawn")


if __name__ == "__main__":
    unittest.main(verbosity=2)
