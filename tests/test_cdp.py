"""CDP 浏览器驱动自测：验证"启动 Edge → 多 frame 执行 JS → 点击 → 截图"链路。

这一步不需要打印机，用的是本地临时 HTML（带 frameset，模拟理光后台的框架结构）。
运行：
    python tests\\test_cdp.py
"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BASE_DIR, os.path.join(BASE_DIR, "vendor")):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

from app.cdp import CdpBrowser, find_browser  # noqa: E402
from app.printer_ricoh import RicohAddressBookRegistrar  # noqa: E402

PAGE = """<html><head><meta charset="utf-8"><title>WIM 模拟页</title></head>
<frameset cols="30%,70%">
  <frame src="left.html" name="left">
  <frame src="right.html" name="right">
</frameset></html>"""

LEFT = """<html><head><meta charset="utf-8"></head><body>
<a href="#" id="nav" onclick="document.getElementById('state').innerText='opened';return false;">设备管理</a>
<div id="state">idle</div>
</body></html>"""

RIGHT = """<html><head><meta charset="utf-8"></head><body>
<h2>Step2.文件夹目的地</h2>
<table><tr><td>协议</td><td><input type="radio" name="p">SMB <input type="radio" name="p">FTP</td></tr>
<tr><td>端口号(必须)</td><td><input type="text" id="port"></td></tr>
<tr><td>服务器名称(必须)</td><td><input type="text" id="server"></td></tr>
<tr><td>路径(必须)</td><td><input type="text" id="path"></td></tr>
<tr><td>登录用户名称</td><td><input type="text" id="user"></td></tr>
</table>
<input type="submit" value="转到下一项">
</body></html>"""


@unittest.skipUnless(find_browser(), "本机没有 Edge/Chrome，跳过")
class CdpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="scanlink-cdp-")
        root = pathlib.Path(cls.tmp.name)
        (root / "page.html").write_text(PAGE, encoding="utf-8")
        (root / "left.html").write_text(LEFT, encoding="utf-8")
        (root / "right.html").write_text(RIGHT, encoding="utf-8")
        cls.url = (root / "page.html").as_uri()
        cls.logs = []
        cls.browser = CdpBrowser(headless=True, log=cls.logs.append)
        cls.browser.start()

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.browser.close()
        except Exception:
            pass
        cls.tmp.cleanup()

    def setUp(self) -> None:
        # 共用一个浏览器实例：每新开一个浏览器都会让 Windows 产生一次失败登录事件
        # （默认 10 次/10 分钟就会锁死本机账户），所以测试里也要复用。
        self.browser = type(self).browser
        self.browser.navigate(self.url, wait_ready=True)

    def test_frames_present(self) -> None:
        # frameset 里的每个 frame 都应该有独立的执行上下文
        self.assertGreaterEqual(len(self.browser.contexts), 2, "没有拿到子 frame 的执行上下文")

    def test_page_text_covers_all_frames(self) -> None:
        text = self.browser.page_text()
        self.assertIn("设备管理", text)
        self.assertIn("Step2.文件夹目的地", text)

    def test_click_in_child_frame(self) -> None:
        registrar = RicohAddressBookRegistrar(host="127.0.0.1", ftp_host="127.0.0.1", step_wait_ms=200)
        registrar.browser = self.browser
        self.assertTrue(registrar._click_text(["设备管理"], fallback=False))
        registrar._wait(0.3)
        self.assertIn("opened", self.browser.page_text())

    def test_fill_inputs_by_label(self) -> None:
        registrar = RicohAddressBookRegistrar(host="127.0.0.1", ftp_host="127.0.0.1", step_wait_ms=200)
        registrar.browser = self.browser
        values = ["21", "192.168.1.11", "/", "anonymous"]
        labels = ["端口号", "服务器名称", "路径", "登录用户名称"]
        filled = registrar._fill_text_inputs(values, labels)
        self.assertEqual(filled, 4, "按 label 定位输入框失败")

        got = self.browser.collect_all(
            """() => {
                const els = [...document.querySelectorAll('input[type="text"]')];
                return els.length ? els.map(e => e.value) : null;
            }"""
        )
        self.assertTrue(got, "读不到输入框的值")
        self.assertEqual(got[0], ["21", "192.168.1.11", "/", "anonymous"])

    def test_click_radio_and_button(self) -> None:
        registrar = RicohAddressBookRegistrar(host="127.0.0.1", ftp_host="127.0.0.1", step_wait_ms=200)
        registrar.browser = self.browser
        self.assertTrue(registrar._click_radio_index(1))
        checked = self.browser.evaluate_all(
            """() => {
                const r = [...document.querySelectorAll('input[type="radio"]')];
                return r.length ? r.map(x => x.checked) : null;
            }"""
        )
        self.assertEqual(checked, [False, True])
        self.assertTrue(registrar._click_next())

    def test_screenshot(self) -> None:
        path = os.path.join(self.tmp.name, "shot.png")
        self.assertTrue(self.browser.screenshot(path))
        self.assertGreater(os.path.getsize(path), 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
