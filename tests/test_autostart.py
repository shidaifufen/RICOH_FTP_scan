"""开机自启测试：注册表写入/删除、命令行内容、自启模式行为。

注意：本测试会真的写 HKCU 的 Run 项，测试结束会恢复原状态。
运行： python tests\\test_autostart.py
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

from app import autostart  # noqa: E402


def _tk_available() -> bool:
    try:
        import tkinter

        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(autostart.is_supported(), "只有 Windows 支持开机自启")
class AutostartRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.was_enabled = autostart.is_enabled()
        self.old_command = autostart.current_command()
        autostart.disable()

    def tearDown(self) -> None:
        # 恢复测试前的状态，避免污染本机
        autostart.disable()
        if self.was_enabled and self.old_command:
            try:
                import winreg

                with winreg.CreateKeyEx(
                    winreg.HKEY_CURRENT_USER, autostart.RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.SetValueEx(
                        key, autostart.VALUE_NAME, 0, winreg.REG_SZ, self.old_command
                    )
            except OSError:
                pass

    def test_enable_disable_roundtrip(self) -> None:
        self.assertFalse(autostart.is_enabled())
        ok, detail = autostart.enable()
        self.assertTrue(ok, detail)
        self.assertTrue(autostart.is_enabled(), "开启后注册表里应该有值")

        cmd = autostart.current_command()
        self.assertIn("--autostart", cmd, f"自启命令缺少 --autostart：{cmd}")
        self.assertTrue(
            "main.py" in cmd or cmd.lower().endswith('--autostart'),
            f"自启命令既没有 main.py 也不是 exe：{cmd}",
        )

        ok, detail = autostart.disable()
        self.assertTrue(ok, detail)
        self.assertFalse(autostart.is_enabled(), "关闭后注册表里不应有值")

    def test_disable_when_already_off_is_ok(self) -> None:
        ok, detail = autostart.disable()
        self.assertTrue(ok, detail)

    def test_toggle(self) -> None:
        ok, now_on, detail = autostart.toggle()
        self.assertTrue(ok, detail)
        self.assertTrue(now_on)
        self.assertTrue(autostart.is_enabled())

        ok, now_on, detail = autostart.toggle()
        self.assertTrue(ok, detail)
        self.assertFalse(now_on)
        self.assertFalse(autostart.is_enabled())


@unittest.skipUnless(_tk_available(), "没有可用桌面会话")
class AutostartGuiTest(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from app.main import App

        self.was_enabled = autostart.is_enabled()
        self.old_command = autostart.current_command()
        autostart.disable()

        self.tmp = tempfile.TemporaryDirectory(prefix="scanlink-autostart-")
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.tmp.name, enable_tray=False)
        self.root.update()

    def tearDown(self) -> None:
        try:
            self.app._stop_ftp()
        except Exception:
            pass
        autostart.disable()
        if self.was_enabled and self.old_command:
            try:
                import winreg

                with winreg.CreateKeyEx(
                    winreg.HKEY_CURRENT_USER, autostart.RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE
                ) as key:
                    winreg.SetValueEx(
                        key, autostart.VALUE_NAME, 0, winreg.REG_SZ, self.old_command
                    )
            except OSError:
                pass
        try:
            self.app._closing = True
            self.root.destroy()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_checkbox_reflects_registry(self) -> None:
        self.assertFalse(self.app.autostart_var.get(), "初始应为未勾选")
        autostart.enable()
        self.app._refresh_autostart_hint()
        self.assertTrue(self.app.autostart_var.get(), "注册表开启后勾选框应同步")
        self.assertIn("已开启", self.app.autostart_hint.cget("text"))

    def test_toggle_from_checkbox(self) -> None:
        self.app.autostart_var.set(True)
        self.app._on_autostart_toggle()
        self.assertTrue(autostart.is_enabled(), "勾选后应写入注册表")

        self.app.autostart_var.set(False)
        self.app._on_autostart_toggle()
        self.assertFalse(autostart.is_enabled(), "取消勾选后应删除注册表项")

    def test_autostart_mode_hides_window_and_starts_ftp(self) -> None:
        """自启模式：托盘可用时隐藏窗口 + 自动启动 FTP（托盘不可用则退回显示窗口）。"""
        import tkinter as tk

        from app.main import App

        root = tk.Tk()
        app = App(root, self.tmp.name, enable_tray=True, autostart_mode=True)
        try:
            root.update()
            tray_ok = app.tray is not None and app.tray.available
            if tray_ok:
                self.assertEqual(root.state(), "withdrawn", "自启模式且托盘可用时不应显示窗口")
            else:
                self.assertEqual(root.state(), "normal", "托盘不可用时应退回显示窗口")
            self.assertIsNotNone(app.ftp, "自启模式应自动启动 FTP")
            self.assertTrue(app.ftp.running)
        finally:
            try:
                app._stop_ftp()
            except Exception:
                pass
            try:
                if app.tray is not None:
                    app.tray.stop()
            except Exception:
                pass
            app._closing = True
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
