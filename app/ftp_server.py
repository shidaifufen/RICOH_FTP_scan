"""内置 FTP 服务（取代教程里的 Slyar FTPServer）。

设计要点（都是踩过坑的地方）：
* **被动模式**必须显式配置数据端口段，并把 `masquerade_address` 设成"打印机可达的
  本机 IP"。否则 pyftpdlib 会在 PASV 响应里回 127.0.0.1 或另一块网卡的地址，
  打印机就会出现"能登录但一上传就超时"的假故障。
* Windows 上关掉 sendfile（该平台未实现）。
* 允许匿名（教程就是这么配的：anonymous / 空密码），也支持自定义账号密码。
* 收到文件时回调 UI，做到"扫完就能看到文件落地"。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import warnings
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from . import netinfo

DEFAULT_PASSIVE_RANGE = range(30000, 30100)
DEFAULT_PERM = "elradfmw"  # e=切目录 l=列目录 r=下载 a=追加 d=删除 f=重命名 m=建目录 w=上传

try:  # pragma: no cover - 依赖 vendor 目录
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    PYFTPDLIB_AVAILABLE = True
except ImportError:  # pragma: no cover
    PYFTPDLIB_AVAILABLE = False


@dataclass
class ReceivedFile:
    path: str
    size: int
    when: float = field(default_factory=time.time)


class _LogBridge(logging.Handler):
    def __init__(self, callback: Optional[Callable[[str], None]]):
        super().__init__()
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        if self._callback:
            try:
                self._callback(record.getMessage())
            except Exception:
                pass


class FtpScanServer:
    """一个可随时启停的扫描接收 FTP 服务。"""

    def __init__(
        self,
        root_dir: str,
        port: int = 21,
        username: str = "anonymous",
        password: str = "",
        perm: str = DEFAULT_PERM,
        passive_ports: range = DEFAULT_PASSIVE_RANGE,
        log: Optional[Callable[[str], None]] = None,
        on_file: Optional[Callable[[ReceivedFile], None]] = None,
    ) -> None:
        self.root_dir = os.path.abspath(root_dir)
        self.port = int(port)
        self.username = (username or "anonymous").strip() or "anonymous"
        self.password = password or ""
        self.perm = perm or DEFAULT_PERM
        self.passive_ports = passive_ports
        self.log = log or (lambda msg: None)
        self.on_file = on_file

        self._server: Optional["FTPServer"] = None
        self._thread: Optional[threading.Thread] = None
        self._handler_cls = None
        self._log_handler: Optional[_LogBridge] = None

        self.masquerade_ip: Optional[str] = None
        self.received: List[ReceivedFile] = []
        self.last_error: str = ""
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ 状态
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_anonymous(self) -> bool:
        return self.username.lower() == "anonymous"

    def status_line(self) -> str:
        if self.running:
            host = self.masquerade_ip or "?"
            return f"运行中 · ftp://{host}:{self.port} · 账号 {self.username}"
        return "未启动"

    # ------------------------------------------------------------------ 生命周期
    def start(self) -> bool:
        if not PYFTPDLIB_AVAILABLE:
            self.last_error = "缺少 pyftpdlib 依赖，请先运行 setup.bat 安装 vendor 依赖"
            self.log(f"[FTP] {self.last_error}")
            return False
        if self.running:
            return True

        if not os.path.isdir(self.root_dir):
            try:
                os.makedirs(self.root_dir, exist_ok=True)
            except OSError as exc:
                self.last_error = f"保存目录不可用：{exc}"
                self.log(f"[FTP] {self.last_error}")
                return False

        self.masquerade_ip = self.masquerade_ip or netinfo.choose_local_ip()

        try:
            authorizer = DummyAuthorizer()
            if self.is_anonymous:
                # 匿名写权限是需求（打印机要上传扫描件），pyftpdlib 会为此发 RuntimeWarning，
                # 这里局部静音，免得日志里出现吓人的告警。
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    authorizer.add_anonymous(self.root_dir, perm=self.perm)
            else:
                authorizer.add_user(self.username, self.password, self.root_dir, perm=self.perm)
        except Exception as exc:  # pragma: no cover
            self.last_error = f"账号配置失败：{exc}"
            self.log(f"[FTP] {self.last_error}")
            return False

        server_self = self

        class ScanHandler(FTPHandler):  # type: ignore[misc, valid-type]
            passive_ports = server_self.passive_ports
            masquerade_address = server_self.masquerade_ip
            use_sendfile = False
            banner = "扫描直连 FTP 服务就绪"
            encoding = "utf-8"
            timeout = 300
            permit_foreign_addresses = True

            def on_file_received(self, file):  # noqa: D102
                try:
                    real = self.fs.fs2os(file)
                except Exception:
                    real = str(file)
                try:
                    size = os.path.getsize(real)
                except OSError:
                    size = 0
                item = ReceivedFile(path=real, size=size)
                with server_self._lock:
                    server_self.received.append(item)
                if server_self.on_file:
                    try:
                        server_self.on_file(item)
                    except Exception:
                        pass

            def on_login(self, username):  # noqa: D102
                server_self.log(f"[FTP] 打印机已登录（用户 {username}，来自 {self.remote_ip}）")

            def on_login_failed(self, username, password):  # noqa: D102
                server_self.log(f"[FTP] 登录失败（用户 {username}，来自 {self.remote_ip}）")

        # 关键：pyftpdlib 的 authorizer 是 handler 的类属性，必须显式挂上去。
        # 否则会退化成默认的空 authorizer，表现就是 "530 Anonymous access not allowed"。
        ScanHandler.authorizer = authorizer

        self._handler_cls = ScanHandler
        self._attach_logger()

        try:
            self._server = FTPServer(("0.0.0.0", self.port), ScanHandler)
            self._server.max_cons = 64
            self._server.max_cons_per_ip = 8
        except OSError as exc:
            self.last_error = (
                f"端口 {self.port} 启动失败：{exc}。"
                "可能是端口被占用（旧 FTP 软件还在跑）或需要更高权限。"
            )
            self.log(f"[FTP] {self.last_error}")
            self._detach_logger()
            return False

        self._thread = threading.Thread(target=self._serve, name="ftp-server", daemon=True)
        self._thread.start()
        time.sleep(0.2)
        if not self.running:  # pragma: no cover
            self.last_error = self.last_error or "FTP 服务未能启动"
            self.log(f"[FTP] {self.last_error}")
            return False

        self.log(
            f"[FTP] 已启动 ftp://{self.masquerade_ip}:{self.port}"
            f"（账号 {self.username}，目录 {self.root_dir}）"
        )
        return True

    def _serve(self) -> None:  # pragma: no cover
        try:
            assert self._server is not None
            self._server.serve_forever(handle_exit=False)
        except Exception as exc:  # 端口冲突、socket 被关闭等
            self.last_error = str(exc)
            self.log(f"[FTP] 服务异常退出：{exc}")

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close_all()
            except Exception:
                pass
        self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._thread = None
        self._detach_logger()
        self.log("[FTP] 已停止")

    # ------------------------------------------------------------------ 助手
    def _attach_logger(self) -> None:
        if self._log_handler is not None:
            return
        logger = logging.getLogger("pyftpdlib")
        logger.setLevel(logging.INFO)
        self._log_handler = _LogBridge(self.log)
        self._log_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(self._log_handler)
        logger.propagate = False

    def _detach_logger(self) -> None:
        if self._log_handler is None:
            return
        logging.getLogger("pyftpdlib").removeHandler(self._log_handler)
        self._log_handler = None

    # ------------------------------------------------------------------ 自检
    def self_check(self) -> tuple[bool, str]:
        """启动后自测：用本机 ftplib 连一下，确认控制/被动连接都通。"""
        import ftplib

        host = "127.0.0.1"
        try:
            with ftplib.FTP() as ftp:
                ftp.connect(host, self.port, timeout=5)
                if self.is_anonymous:
                    ftp.login()
                else:
                    ftp.login(self.username, self.password)
                names = ftp.nlst()
                ftp.set_pasv(True)
                ftp.retrlines("LIST", lambda line: None)
            return True, f"自检通过（目录 {len(names)} 项）"
        except (ftplib.all_errors + (socket.timeout,)) as exc:  # type: ignore[operator]
            return False, f"自检失败：{exc}"
