"""自动注册流程端到端测试：对着「假理光后台」把教程那 10 步全跑一遍。

这是没有真打印机时最接近实战的验证：登录 → 通讯簿 → 添加用户 → 勾选目的地
→ Step1 基本信息 → Step2 文件夹目的地 → Step3 确认注册 → 回列表校验。

运行：
    python tests\\test_registration.py
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

from app.cdp import CdpBrowser, find_browser  # noqa: E402
from app.printer_ricoh import RicohAddressBookRegistrar  # noqa: E402
from tests.mock_wim import ADMIN_PASSWORD, MockWimServer  # noqa: E402


@unittest.skipUnless(find_browser(), "本机没有 Edge/Chrome，跳过")
class RegistrationFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 一个测试文件只启动一个浏览器：避免反复触发 Windows 失败登录
        cls.shared_logs = []
        cls.shared_browser = CdpBrowser(headless=True, log=cls.shared_logs.append)
        cls.shared_browser.start()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.shared_browser.close()
        except Exception:
            pass

    def setUp(self) -> None:
        self.server = MockWimServer(port=0)
        self.server.start()
        self.tmp = tempfile.TemporaryDirectory(prefix="scanlink-reg-")
        self.logs = []

    def tearDown(self) -> None:
        self.server.stop()
        self.tmp.cleanup()

    def _registrar(self, password: str = ADMIN_PASSWORD, **kwargs) -> RicohAddressBookRegistrar:
        return RicohAddressBookRegistrar(
            host=f"127.0.0.1:{self.server.port}",
            admin_password=password,
            ftp_host="192.168.1.10",
            ftp_port=21,
            ftp_user="anonymous",
            ftp_path="/",
            headless=True,
            step_wait_ms=400,
            log=self.logs.append,
            debug_dir=os.path.join(self.tmp.name, "debug"),
            browser=self.shared_browser,
            **kwargs,
        )

    # ------------------------------------------------------------------ 用例
    def test_full_registration(self) -> None:
        before = self.server.entry_count
        result = self._registrar().register("张三")

        self.assertTrue(result.ok, f"{result.message}\n{result.summary()}")
        self.assertEqual(self.server.entry_count, before + 1, "通讯簿没有新增条目")

        entry = self.server.find_entry("张三")
        self.assertIsNotNone(entry, "通讯簿里找不到新条目")
        assert entry is not None
        self.assertEqual(entry.protocol, "FTP", "协议没写对（应为 FTP）")
        self.assertEqual(entry.port, "21")
        self.assertEqual(entry.server, "192.168.1.10", "服务器名称没写成本机 IP")
        self.assertEqual(entry.path, "/")
        self.assertEqual(entry.username, "anonymous")
        self.assertEqual(entry.key_display, "张三", "按键显示应等于名字")
        # 自动分配编号：现有最大 00007 → 00008
        self.assertEqual(entry.no, "00008")
        self.assertEqual(result.entry_no, "00008")
        # 关键步骤都留下了记录
        names = [s.name for s in result.steps]
        for expected in ("管理员登录", "进入通讯簿", "打开「添加用户」向导", "填写基本信息", "填写文件夹目的地"):
            self.assertIn(expected, names)

    def test_same_name_updates_instead_of_adding(self) -> None:
        """同名再注册一次：只保留一条，且是新建的那条（编号会变）。"""
        first = self._registrar().register("张三")
        self.assertTrue(first.ok, first.message)
        entry = self.server.find_entry("张三")
        assert entry is not None
        first_no = entry.no

        second = self._registrar().register("张三")
        self.assertTrue(second.ok, second.message)
        self.assertIn("更新", second.message)

        zhangs = [e for e in self.server.state.entries if e.name == "张三"]
        self.assertEqual(len(zhangs), 1, "同名注册后应该只剩一条记录")
        self.assertNotEqual(zhangs[0].no, first_no, "更新后应生成新的注册编号")
        self.assertEqual(zhangs[0].server, "192.168.1.10")

    def test_same_ip_updates_instead_of_adding(self) -> None:
        """同一个 FTP 服务器 IP（= 同一台电脑）视为同一用户：换名字也只留最新一条。"""
        self.assertTrue(self._registrar().register("张三").ok)
        self.assertTrue(self._registrar().register("李四").ok)

        mine = [e for e in self.server.state.entries if e.server == "192.168.1.10"]
        self.assertEqual(len(mine), 1, "同一 IP 只应保留一条记录")
        self.assertEqual(mine[0].name, "李四")
        self.assertIsNone(self.server.find_entry("张三"), "旧名字的条目应已被删除")

    def test_merge_by_ip_can_be_disabled(self) -> None:
        """关掉按 IP 合并后，不同名字应各自保留一条。"""
        self.assertTrue(self._registrar(merge_by_ip=False).register("张三").ok)
        self.assertTrue(self._registrar(merge_by_ip=False).register("李四").ok)
        self.assertIsNotNone(self.server.find_entry("张三"))
        self.assertIsNotNone(self.server.find_entry("李四"))

    def test_wrong_admin_password(self) -> None:
        result = self._registrar(password="wrong-pass").register("李四")
        self.assertFalse(result.ok)
        self.assertIn("登录失败", result.message)
        self.assertIsNone(self.server.find_entry("李四"))

    def test_explicit_entry_number(self) -> None:
        result = self._registrar().register("王五", entry_no="123")
        self.assertTrue(result.ok, result.message)
        entry = self.server.find_entry("王五")
        assert entry is not None
        self.assertEqual(entry.no, "00123")

    def test_failure_leaves_debug_snapshot(self) -> None:
        result = self._registrar(password="wrong-pass").register("赵六")
        self.assertFalse(result.ok)
        debug_dir = os.path.join(self.tmp.name, "debug")
        pngs = [f for f in os.listdir(debug_dir) if f.endswith(".png")] if os.path.isdir(debug_dir) else []
        htmls = [f for f in os.listdir(debug_dir) if f.endswith(".html")] if os.path.isdir(debug_dir) else []
        self.assertTrue(pngs, "失败时没有保存截图")
        self.assertTrue(htmls, "失败时没有保存 HTML 快照")


if __name__ == "__main__":
    unittest.main(verbosity=2)
