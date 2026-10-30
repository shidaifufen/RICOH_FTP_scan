"""极简 Chrome DevTools Protocol 客户端（纯标准库）。

为什么不直接用 Playwright / Selenium：
* Playwright 的 pip 包要额外下载 ~40MB 驱动；Selenium 要下载 msedgedriver。
* 这里只需要"打开页面 + 在页面里跑 JS + 截图"，CDP 的 `Runtime.evaluate` /
  `Page.captureScreenshot` 就够，Windows 自带的 Edge 就支持远程调试。

关键设计（都是踩过的坑）：
1. 用 `--remote-debugging-port=0` 让浏览器自选端口，从
   `<user-data-dir>/DevToolsActivePort` 读回端口；
2. 新版 Chromium 的 `/devtools/browser` 端点要带 target uuid，直接连会 404，
   所以改连 `/json/list` 里 page target 的 `webSocketDebuggerUrl`；
3. **CDP 的事件只在"等命令响应"时才会被读出来**，页面跳转是事件通知的，
   所以每次操作后都要 `pump()` 一下，否则会一直对着已销毁的上下文干活；
4. 理光后台是 frameset，**子 frame 的上下文在导航后不一定会重新创建**。
   因此按 `Page.getFrameTree` + `Page.createIsolatedWorld` 给每个 frame
   兜底建一个隔离世界上下文 —— JS 只操作 DOM，隔离世界完全够用。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


class CdpError(RuntimeError):
    pass


def find_browser(prefer: str = "edge") -> Optional[str]:
    candidates = (CHROME_PATHS + EDGE_PATHS) if prefer == "chrome" else (EDGE_PATHS + CHROME_PATHS)
    for path in candidates:
        if os.path.isfile(path):
            return path
    for name in ("msedge.exe", "chrome.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


# --------------------------------------------------------------------------- WebSocket
class WebSocketClient:
    """只实现 CDP 需要的最小 WebSocket 客户端（RFC6455，客户端掩码）。"""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        if not url.startswith("ws://"):
            raise CdpError(f"不支持的 WebSocket 地址：{url}")
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.host = host or "127.0.0.1"
        self.port = int(port or 80)
        self.path = "/" + path
        self.timeout = timeout
        self._sock: Optional[socket.socket] = None
        self._buf = bytearray()
        self._closed = False

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(request.encode())
        header = self._read_http_header(sock)
        status_line = header.split("\r\n", 1)[0]
        if "101" not in status_line:
            sock.close()
            raise CdpError(f"WebSocket 握手失败：{status_line}")
        self._sock = sock

    @staticmethod
    def _read_http_header(sock: socket.socket) -> str:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
        return data.decode("latin-1", errors="ignore")

    def _send_frame(self, payload: bytes, opcode: int = 0x1) -> None:
        assert self._sock is not None
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def _recv_exact(self, n: int) -> bytes:
        assert self._sock is not None
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                self._closed = True
                raise CdpError("WebSocket 连接被关闭")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def recv_message(self) -> str:
        while True:
            first2 = self._recv_exact(2)
            fin = first2[0] & 0x80
            opcode = first2[0] & 0x0F
            masked = first2[1] & 0x80
            length = first2[1] & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))

            if opcode == 0x8:
                self._closed = True
                raise CdpError("WebSocket 收到 close 帧")
            if opcode == 0x9:
                self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            if opcode in (0x1, 0x2) and fin:
                return payload.decode("utf-8", errors="replace")

    def send_text(self, text: str) -> None:
        self._send_frame(text.encode("utf-8"), opcode=0x1)

    def close(self) -> None:
        try:
            if self._sock is not None and not self._closed:
                self._send_frame(b"", opcode=0x8)
        except Exception:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:
            pass
        self._sock = None
        self._closed = True


# --------------------------------------------------------------------------- CDP
@dataclass
class FrameInfo:
    frame_id: str
    context_id: int
    url: str = ""


class CdpBrowser:
    """启动一个可远程调试的 Edge/Chrome，提供 evaluate / screenshot 能力。"""

    def __init__(
        self,
        headless: bool = True,
        prefer: str = "edge",
        timeout: float = 30.0,
        extra_args: Optional[Sequence[str]] = None,
        log: Optional[Callable[[str], None]] = None,
        profile_dir: Optional[str] = None,
        verbose: bool = False,
    ) -> None:
        self.headless = headless
        self.prefer = prefer
        self.timeout = timeout
        self.extra_args = list(extra_args or [])
        self.log = log or (lambda msg: None)
        self.verbose = verbose
        self._profile_dir = profile_dir or tempfile.mkdtemp(prefix="scanlink-browser-")
        self._owns_profile = profile_dir is None
        self._proc: Optional[subprocess.Popen] = None
        self._ws: Optional[WebSocketClient] = None
        self._port = 0
        self._msg_id = 0

        # frameId -> 执行上下文 id（默认上下文优先，缺失时用隔离世界兜底）
        self.frame_contexts: Dict[str, int] = {}
        self._context_urls: Dict[int, str] = {}
        self.main_frame_id = ""
        self._dirty = True
        self._events: List[dict] = []

    # ------------------------------------------------------------------ 生命周期
    def _launch_args(self, exe: str) -> list:
        args = [
            exe,
            "--remote-debugging-port=0",
            f"--user-data-dir={self._profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-features=Translate,MediaRouter,Sync,msEdgeIdentity",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-gpu-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--disable-sync",
            "--no-service-autorun",
            "--disable-component-extensions-with-background-pages",
            # 打印机/本机都是内网地址，强制直连，避免企业代理拦掉
            "--proxy-server=direct://",
            "--proxy-bypass-list=*",
            "--window-size=1366,900",
            "about:blank",
        ]
        if self.headless:
            args.insert(1, "--headless=new")
        args.extend(self.extra_args)
        return args

    def _launch_once(self, exe: str) -> tuple:
        """启动一个浏览器；返回 (进程, 调试端口)。失败抛 CdpError（带退出码/报错）。"""
        port_file = os.path.join(self._profile_dir, "DevToolsActivePort")
        if os.path.exists(port_file):
            try:
                os.remove(port_file)
            except OSError:
                pass
        self._stderr_path = os.path.join(
            tempfile.gettempdir(), f"scanlink-browser-{os.getpid()}.err"
        )
        err_handle = open(self._stderr_path, "wb")
        creationflags = 0
        if sys.platform.startswith("win"):
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if self.headless else 0
        proc = subprocess.Popen(
            self._launch_args(exe),
            stdout=subprocess.DEVNULL,
            stderr=err_handle,
            creationflags=creationflags,
        )
        self._err_handle = err_handle

        deadline = time.time() + self.timeout
        port = 0
        while time.time() < deadline:
            if os.path.exists(port_file):
                try:
                    with open(port_file, "r", encoding="utf-8") as fh:
                        port = int((fh.readline() or "0").strip() or 0)
                    if port:
                        return proc, port
                except (OSError, ValueError):
                    pass
            code = proc.poll()
            if code is not None:
                detail = self._read_stderr()
                raise CdpError(
                    f"浏览器启动后立即退出（{os.path.basename(exe)}，退出码 {code}）"
                    + (f"；报错：{detail}" if detail else "")
                )
            time.sleep(0.15)
        raise CdpError(f"浏览器没有在 {self.timeout:.0f} 秒内开放调试端口（{os.path.basename(exe)}）")

    def _read_stderr(self) -> str:
        path = getattr(self, "_stderr_path", "")
        if not path or not os.path.isfile(path):
            return ""
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                lines = [line.strip() for line in fh.read().splitlines() if line.strip()]
        except OSError:
            return ""
        text = " | ".join(lines[-3:])
        return text[:300]

    def start(self) -> None:
        """启动浏览器，尽量不死：

        1. 优先 Edge，起不来换 Chrome；
        2. 都起不来就**去掉无头模式**再试一遍（有些企业策略/显卡环境会拦 headless，
           弹个浏览器窗口总比功能不可用好）。
        """
        names = ["chrome", "edge"] if self.prefer == "chrome" else ["edge", "chrome"]
        wanted_headless = self.headless
        plan = [(name, wanted_headless) for name in names]
        if wanted_headless:
            plan += [(name, False) for name in names]
        errors = []
        for name, headless in plan:
            exe = find_browser(name)
            if not exe:
                errors.append(f"{name}: 未安装")
                continue
            self.headless = headless
            self._profile_dir = tempfile.mkdtemp(prefix="scanlink-browser-")
            self._owns_profile = True
            try:
                proc, port = self._launch_once(exe)
            except CdpError as exc:
                errors.append(f"{name}{'(有窗口)' if not headless else ''}: {exc}")
                self.log(f"[浏览器] {name} 启动失败：{exc}")
                continue
            self._proc = proc
            self._port = port
            self.prefer = name
            mode = "无头" if headless else "有窗口"
            self.log(f"[浏览器] 已启动：{os.path.basename(exe)}（{mode}，调试端口 {port}）")
            self._attach_page_target()
            return
        self.headless = wanted_headless
        raise CdpError("没有可用的浏览器：" + "；".join(errors))

    # ------------------------------------------------------------------ 连接
    def _http_json(self, path: str, method: str = "GET") -> Any:
        request = urllib.request.Request(f"http://127.0.0.1:{self._port}{path}", method=method)
        with urllib.request.urlopen(request, timeout=8) as resp:
            return json.loads(resp.read().decode("utf-8", errors="ignore"))

    def _targets(self) -> List[dict]:
        try:
            data = self._http_json("/json/list")
            return data if isinstance(data, list) else []
        except Exception:
            return []

    INTERNAL_PREFIXES = ("edge://", "chrome://", "devtools://", "about:blank#", "chrome-error://")

    @classmethod
    def _is_usable_page(cls, target: dict) -> bool:
        if target.get("type") != "page":
            return False
        url = (target.get("url") or "").strip().lower()
        if url == "about:blank":
            return True
        if url.startswith(("http://", "https://")):
            return True
        return not url.startswith(cls.INTERNAL_PREFIXES)

    def _pick_page_ws(self) -> str:
        for target in self._targets():
            if self._is_usable_page(target):
                ws = target.get("webSocketDebuggerUrl", "") or ""
                if ws:
                    return ws
        return ""

    def _attach_page_target(self) -> None:
        """连到一个普通页面。

        踩过的坑：新 profile 的 Edge 会先弹 `edge://sync-confirmation-dialog/`，
        如果连到它，`Page.enable` 会超时 30 秒（用户现场就是这么失败的）。
        所以这里只认 http/about:blank 页面，必要时自己 PUT /json/new 开一个。
        """
        deadline = time.time() + max(self.timeout, 30.0)
        page_ws = ""
        while time.time() < deadline:
            page_ws = self._pick_page_ws()
            if page_ws:
                break
            try:
                self._http_json("/json/new?about:blank", method="PUT")
            except Exception:
                pass
            time.sleep(0.3)
        if not page_ws:
            pages = [(t.get("type"), (t.get("url") or "")[:60]) for t in self._targets()]
            self.log(f"[浏览器] 可用页面为空，当前 targets={pages}")
            raise CdpError("浏览器没有可用的正常页面标签（只有内部页面）")
        self.log(f"[浏览器] 准备连接页面：{page_ws[:80]}")
        targets = [(t.get("type"), (t.get("url") or "")[:70]) for t in self._targets()]
        self.log(f"[浏览器] 当前 targets：{targets}")

        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                self._ws = WebSocketClient(page_ws, timeout=self.timeout)
                self._ws.connect()
                # 先用 Browser.getVersion 探一下 WS 通道本身通不通
                try:
                    version = self.send("Browser.getVersion", timeout=8)
                    self.log(f"[浏览器] WS 通道正常，Browser={str(version.get('product'))[:40]}")
                except CdpError as exc:
                    self.log(f"[浏览器] WS 通道异常（Browser.getVersion 失败）：{exc}")
                self.send("Page.enable", timeout=45)
                self.send("Runtime.enable", timeout=45)
                self.send("Page.setLifecycleEventsEnabled", {"enabled": True})
                self.pump()
                return
            except (CdpError, OSError) as exc:
                last_error = exc
                self.log(f"[浏览器] 连接页面失败（第 {attempt + 1} 次）：{exc}")
                self._diagnose()
                # 关掉旧连接，重新挑一个页面再试
                try:
                    if self._ws is not None:
                        self._ws.close()
                except Exception:
                    pass
                self._ws = None
                time.sleep(1.0)
                try:
                    self._http_json("/json/new?about:blank", method="PUT")
                except Exception:
                    pass
                page_ws = self._pick_page_ws()
                if not page_ws:
                    time.sleep(0.5)
                    page_ws = self._pick_page_ws()
                if not page_ws:
                    break
        raise CdpError(f"连不上浏览器页面：{last_error}")

    def is_alive(self) -> bool:
        """浏览器进程和调试连接是否都还活着（用于复用/重建判断）。"""
        if self._ws is None or self._ws._closed:
            return False
        if self._proc is None or self._proc.poll() is not None:
            return False
        return True

    def _diagnose(self) -> None:
        """页面级命令不通时，判断是"浏览器活着但渲染进程没起来"还是别的。"""
        try:
            self.log(f"[诊断] 浏览器进程存活: {self._proc is not None and self._proc.poll() is None}")
        except Exception:
            pass
        try:
            targets = self.send("Target.getTargets", timeout=8).get("targetInfos", [])
            brief = [(t.get("type"), (t.get("url") or "")[:50], t.get("attached")) for t in targets]
            self.log(f"[诊断] Target.getTargets 正常，共 {len(brief)} 个：{brief[:6]}")
        except CdpError as exc:
            self.log(f"[诊断] Target.getTargets 失败：{exc}")
        for label, method, params in (
            ("Runtime.evaluate(无context)", "Runtime.evaluate",
             {"expression": "1+1", "returnByValue": True}),
            ("Page.getFrameTree", "Page.getFrameTree", None),
        ):
            try:
                result = self.send(method, params, timeout=8)
                self.log(f"[诊断] {label} 成功：{str(result)[:80]}")
            except CdpError as exc:
                self.log(f"[诊断] {label} 失败：{exc}")

    def close(self) -> None:
        try:
            if self._ws is not None and not self._ws._closed:
                try:
                    self.send("Browser.close", timeout=3)
                except Exception:
                    pass
        finally:
            if self._ws is not None:
                self._ws.close()
            if self._proc is not None:
                pid = self._proc.pid
                try:
                    if sys.platform.startswith("win"):
                        # /T 连子进程一起杀：Edge 会派生一堆 renderer/utility 进程，
                        # 只结束父进程会留下僵尸，占着打印机后台会话
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(pid)],
                            capture_output=True,
                            timeout=15,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        )
                    self._proc.terminate()
                    self._proc.wait(timeout=5)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
            if self._owns_profile:
                shutil.rmtree(self._profile_dir, ignore_errors=True)

    # ------------------------------------------------------------------ 命令收发
    def send(self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        if self._ws is None:
            raise CdpError("浏览器未启动")
        if self.verbose:
            self.log(f"[CDP] -> {method}")
        self._msg_id += 1
        msg_id = self._msg_id
        payload: Dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        self._ws.send_text(json.dumps(payload))

        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            try:
                raw = self._ws.recv_message()
            except socket.timeout:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if self.verbose and message.get("id") is None:
                self.log(f"[CDP] <- event {message.get('method', '?')}")
            if message.get("id") == msg_id:
                if self.verbose:
                    self.log(f"[CDP] <- {method} 响应 ok" if "error" not in message else f"[CDP] <- {method} 报错")
                if "error" in message:
                    raise CdpError(f"{method} 失败：{message['error']}")
                return message.get("result", {})
            self._handle_event(message)
        raise CdpError(f"{method} 超时（{timeout or self.timeout}s）")

    def _handle_event(self, message: dict) -> None:
        method = message.get("method", "")
        params = message.get("params", {}) or {}
        if method == "Runtime.executionContextCreated":
            context = params.get("context", {}) or {}
            aux = context.get("auxData", {}) or {}
            context_id = int(context.get("id", 0) or 0)
            if aux.get("frameId"):
                self._context_urls[context_id] = context.get("origin", "")
                if aux.get("isDefault"):
                    self.frame_contexts[aux["frameId"]] = context_id
                    if not self.main_frame_id:
                        self.main_frame_id = aux["frameId"]
            return
        if method == "Runtime.executionContextDestroyed":
            context_id = int(params.get("executionContextId", 0) or 0)
            self._context_urls.pop(context_id, None)
            for frame_id, cid in list(self.frame_contexts.items()):
                if cid == context_id:
                    del self.frame_contexts[frame_id]
                    self._dirty = True
            return
        if method == "Runtime.executionContextsCleared":
            # 不要整体 clear：子 frame 可能并没有跟着重建上下文。
            # 标脏，下次 pump 用 getFrameTree 校验并按需补隔离世界。
            self._dirty = True
            return
        if method == "Page.frameNavigated":
            frame = params.get("frame", {}) or {}
            if not frame.get("parentId") and frame.get("id"):
                self.main_frame_id = frame["id"]
            self._dirty = True
            return
        if method == "Page.javascriptDialogOpening":
            self.log(f"[浏览器] 页面弹窗：{params.get('message', '')}")
            try:
                self.send("Page.handleJavaScriptDialog", {"accept": True}, timeout=5)
            except Exception:
                pass
            return
        self._events.append(message)

    # ------------------------------------------------------------------ 上下文
    @property
    def contexts(self) -> Dict[int, FrameInfo]:
        """兼容旧接口：contextId -> FrameInfo。"""
        return {
            cid: FrameInfo(frame_id=fid, context_id=cid, url=self._context_urls.get(cid, ""))
            for fid, cid in self.frame_contexts.items()
        }

    def _flatten_frames(self, node: dict) -> List[str]:
        frames: List[str] = []
        frame = (node or {}).get("frame") or {}
        if frame.get("id"):
            frames.append(frame["id"])
        for child in (node or {}).get("childFrames") or []:
            frames.extend(self._flatten_frames(child))
        return frames

    def pump(self, timeout: float = 3.0) -> None:
        """空转一次命令，把 socket 里堆积的事件消费掉；必要时补建 frame 上下文。"""
        try:
            tree = self.send("Page.getFrameTree", timeout=timeout)
        except (CdpError, socket.timeout):
            return
        if not self._dirty and self.frame_contexts:
            return
        frame_ids = self._flatten_frames(tree.get("frameTree") or {})
        if frame_ids and not self.main_frame_id:
            self.main_frame_id = frame_ids[0]
        for frame_id in frame_ids:
            if self.frame_contexts.get(frame_id):
                continue
            try:
                created = self.send(
                    "Page.createIsolatedWorld",
                    {"frameId": frame_id, "worldName": "scanlink", "grantUniveralAccess": False},
                    timeout=timeout,
                )
            except CdpError:
                continue
            context_id = int(created.get("executionContextId", 0) or 0)
            if context_id:
                self.frame_contexts[frame_id] = context_id
        self._dirty = False

    def main_context_id(self) -> Optional[int]:
        if self.main_frame_id and self.main_frame_id in self.frame_contexts:
            return self.frame_contexts[self.main_frame_id]
        if self.frame_contexts:
            return next(iter(self.frame_contexts.values()))
        return None

    def _target_contexts(self) -> List[int]:
        return list(self.frame_contexts.values())

    def _drop_context(self, context_id: int) -> None:
        for frame_id, cid in list(self.frame_contexts.items()):
            if cid == context_id:
                del self.frame_contexts[frame_id]
        self._dirty = True

    # ------------------------------------------------------------------ 页面能力
    def navigate(self, url: str, wait_ready: bool = True, ready_timeout: float = 25.0) -> None:
        self.frame_contexts.clear()
        self._context_urls.clear()
        self._dirty = True
        self.send("Page.navigate", {"url": url})
        self.pump()
        if wait_ready:
            self.wait_ready(ready_timeout)

    def wait_ready(self, timeout: float = 25.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.pump(timeout=2.0)
            context_id = self.main_context_id()
            if context_id is not None:
                state = self.evaluate("document.readyState", context_id=context_id)
                if state == "complete":
                    time.sleep(0.35)
                    return
            time.sleep(0.2)
        time.sleep(0.5)

    def wait(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

    def _eval_in(self, context_id: int, expression: str, arg: Any = None, call: bool = False) -> Any:
        """在指定上下文执行 JS。

        * call=True：把 expression 当函数并立刻调用；
        * 传了 arg：同样按 "函数 + 参数" 调用；
        * 都不传：expression 就是普通表达式。
        """
        if call or arg is not None:
            literal = json.dumps(arg, ensure_ascii=False) if arg is not None else ''
            expression = f"({expression})({literal})"
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "contextId": context_id,
                "returnByValue": True,
                "awaitPromise": False,
                "userGesture": True,
            },
        )
        if "exceptionDetails" in result:
            return None
        return (result.get("result") or {}).get("value")

    def evaluate(self, expression: str, context_id: Optional[int] = None, use_first_context: bool = False) -> Any:
        """在指定上下文（或所有 frame）执行 JS，返回第一个非空结果。"""
        if context_id is not None:
            candidates = [context_id]
        else:
            self.pump()
            main = self.main_context_id()
            candidates = [main] if (use_first_context and main is not None) else self._target_contexts()

        for attempt in range(2):
            for cid in list(candidates):
                try:
                    value = self._eval_in(cid, expression)
                except CdpError:
                    self._drop_context(cid)
                    continue
                if value is None or value is False:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                return value
            if attempt == 0:
                before = sorted(self._target_contexts())
                self.pump()
                main = self.main_context_id()
                if context_id is not None:
                    candidates = [context_id] if context_id in self.frame_contexts.values() else []
                else:
                    candidates = [main] if (use_first_context and main is not None) else self._target_contexts()
                if sorted(self._target_contexts()) == before and context_id is None:
                    break
        return None

    def evaluate_all(self, js_function: str, arg: Any = None) -> Any:
        expression = f"({js_function})({json.dumps(arg, ensure_ascii=False)})"
        return self.evaluate(expression)

    def collect_all(self, js_function: str, arg: Any = None) -> List[Any]:
        expression = f"({js_function})({json.dumps(arg, ensure_ascii=False)})"
        self.pump()
        out: List[Any] = []
        for cid in list(self._target_contexts()):
            try:
                value = self._eval_in(cid, expression)
            except CdpError:
                self._drop_context(cid)
                continue
            if value:
                out.append(value)
        return out

    def page_text(self) -> str:
        """拼接所有 frame 的可见文字（理光后台：菜单在左帧、内容在右帧）。"""
        chunks = self.collect_all("() => document.body ? document.body.innerText : ''")
        return "\n".join(str(chunk) for chunk in chunks if chunk)

    def screenshot(self, path: str) -> bool:
        try:
            result = self.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True})
            data = result.get("data", "")
            if not data:
                return False
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(data))
            return True
        except Exception:
            return False

    def html_dump(self) -> str:
        chunks: List[str] = []
        for cid in list(self._target_contexts()):
            try:
                html = self._eval_in(cid, "document.documentElement ? document.documentElement.outerHTML : ''")
            except CdpError:
                continue
            chunks.append(f"<!-- context {cid} -->\n{html or ''}")
        return "\n\n".join(chunks)
