"""FTP 服务端到端自测：不依赖打印机，直接验证扫描件能不能收进来。

运行：
    python tests\\test_ftp.py
"""

from __future__ import annotations

import ftplib
import os
import sys
import tempfile
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BASE_DIR, os.path.join(BASE_DIR, "vendor")):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

from app.ftp_server import FtpScanServer  # noqa: E402


class FtpServerTest(unittest.TestCase):
    PORT = 2121

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="scanlink-ftp-")
        self.received = []
        self.logs = []
        self.server = FtpScanServer(
            root_dir=self.tmp.name,
            port=self.PORT,
            passive_ports=range(31000, 31010),
            log=self.logs.append,
            on_file=self.received.append,
        )
        self.assertTrue(self.server.start(), self.server.last_error)

    def tearDown(self) -> None:
        self.server.stop()
        self.tmp.cleanup()

    def test_status(self) -> None:
        self.assertTrue(self.server.running)
        self.assertIn("运行中", self.server.status_line())

    def test_upload_like_printer_scan(self) -> None:
        """模拟打印机：登录 → 被动模式 → STOR 大文件（跨多个数据包）→ LIST。"""
        payload = os.urandom(300 * 1024)
        with ftplib.FTP() as ftp:
            ftp.connect("127.0.0.1", self.PORT, timeout=10)
            ftp.login()
            ftp.set_pasv(True)
            remote = "20260430_扫描件.pdf"
            result = ftp.storbinary(f"STOR {remote}", _Bytes(payload))
            self.assertTrue(result.startswith("226") or result.startswith("250"), result)
            names = ftp.nlst()
        self.assertIn(remote, names)
        local = os.path.join(self.tmp.name, remote)
        self.assertTrue(os.path.isfile(local))
        self.assertEqual(os.path.getsize(local), len(payload))
        self.assertTrue(self.received, "on_file_received 回调没有触发")
        self.assertEqual(self.received[0].path, local)

    def test_download_and_list(self) -> None:
        with open(os.path.join(self.tmp.name, "已有文件.txt"), "w", encoding="utf-8") as fh:
            fh.write("hello")
        with ftplib.FTP() as ftp:
            ftp.connect("127.0.0.1", self.PORT, timeout=10)
            ftp.login()
            ftp.set_pasv(True)
            lines = []
            ftp.retrlines("LIST", lines.append)
            self.assertTrue(any("已有文件.txt" in line for line in lines), lines)
            buffer = bytearray()
            ftp.retrbinary("RETR 已有文件.txt", buffer.extend)
        self.assertEqual(bytes(buffer), "hello".encode("utf-8"))

    def test_self_check(self) -> None:
        ok, detail = self.server.self_check()
        self.assertTrue(ok, detail)

    def test_wrong_port_reports_error(self) -> None:
        other = FtpScanServer(root_dir=self.tmp.name, port=self.PORT, log=self.logs.append)
        self.assertFalse(other.start())
        self.assertIn("端口", other.last_error)


class _Bytes:
    """ftplib.storbinary 需要一个有 read 方法的文件对象。"""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


if __name__ == "__main__":
    unittest.main(verbosity=2)
