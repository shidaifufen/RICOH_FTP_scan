"""界面烟雾测试：能不能正常建起窗口、各控件在不在、日志能不能刷出来。

注意：这里不跑 mainloop，只跑几次 update()，避免测试卡死。
运行：
    python tests\\test_gui.py
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


@unittest.skipUnless(_tk_available(), "当前环境没有可用桌面/图形会话")
class GuiSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from app.main import App

        self.tmp = tempfile.TemporaryDirectory(prefix="scanlink-gui-")
        self.root = tk.Tk()
        self.root.withdraw()  # 测试时不真的弹窗
        self.app = App(self.root, self.tmp.name, enable_tray=False)
        self.root.update()

    def tearDown(self) -> None:
        try:
            self.app._stop_ftp()
        except Exception:
            pass
        self.root.destroy()
        self.tmp.cleanup()

    def test_widgets_exist(self) -> None:
        self.assertEqual(self.app.name_var.get(), "")
        self.assertTrue(self.app.dir_var.get(), "保存目录应有默认值")
        self.assertEqual(self.app.port_var.get(), "21")
        self.assertTrue(self.app.anon_var.get(), "默认应为匿名访问")

    def test_config_roundtrip(self) -> None:
        self.app.name_var.set("张三")
        self.app.printer_var.set("192.168.1.100")
        self.app.admin_pwd_var.set("pw")
        cfg = self.app._collect_config()

        from app.config import load_config, save_config

        save_config(self.tmp.name, cfg)
        loaded = load_config(self.tmp.name)
        self.assertEqual(loaded.last_name, "张三")
        self.assertEqual(loaded.printer_host, "192.168.1.100")
        self.assertEqual(loaded.admin_password, "pw")

    def test_log_shows_up(self) -> None:
        self.app.log("测试日志一行")
        for _ in range(20):
            self.root.update()
            import time

            time.sleep(0.02)
        content = self.app.log_text.get("1.0", "end")
        self.assertIn("测试日志一行", content)

    def test_start_and_stop_ftp(self) -> None:
        # 用非 21 端口，避免和真 FTP 冲突
        self.app.port_var.set("2322")
        self.app.dir_var.set(os.path.join(self.tmp.name, "扫描件"))
        self.assertTrue(self.app._start_ftp(silent=True), "FTP 没能启动")
        self.assertIsNotNone(self.app.ftp)
        self.assertTrue(self.app.ftp.running)
        ok, detail = self.app.ftp.self_check()
        self.assertTrue(ok, detail)
        self.app._stop_ftp()
        self.assertIsNone(self.app.ftp)

    def test_local_ip_detected(self) -> None:
        self.app._refresh_local_ip()
        self.assertTrue(self.app._current_ip(), "没有检测到本机 IP")

    def test_ftp_state_label_follows_server_state(self) -> None:
        """回归：FTP 启动成功后，界面上的「● 已启动/未启动」必须跟着变（之前只变颜色不变文字）。"""
        self.assertEqual(self.app.ftp_state.cget("text"), "● 未启动")
        self.app.port_var.set("2323")
        self.app.dir_var.set(os.path.join(self.tmp.name, "扫描件"))
        self.assertTrue(self.app._start_ftp(silent=True), "FTP 没能启动")
        self.assertEqual(self.app.ftp_state.cget("text"), "● 已启动")
        self.app._stop_ftp()
        self.assertEqual(self.app.ftp_state.cget("text"), "● 未启动")


if __name__ == "__main__":
    unittest.main(verbosity=2)
