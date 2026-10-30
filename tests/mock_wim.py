"""假理光 Web Image Monitor —— 用于在没打印机的情况下端到端验证自动注册流程。

页面结构按教程截图复刻（frameset 左菜单 + 右内容、中文标签、三步向导）：
    未登录欢迎页 →「登录」→ 登录表单 → 地址列表（简易输入/添加用户）
    → 更改注册（勾选基本信息 + 文件夹目的地）→ Step1 基本信息
    → Step2 文件夹目的地 → Step3 确认注册 → 确定 → 回地址列表

只实现自动化会用到的元素，够用来回归 `app/printer_ricoh.py` 的每一步。
"""

from __future__ import annotations

import html
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple

MODEL = "MP C4504"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "secret"


@dataclass
class MockEntry:
    no: str = ""
    name: str = ""
    key_display: str = ""
    protocol: str = ""
    port: str = ""
    server: str = ""
    path: str = ""
    username: str = ""
    password: str = ""


@dataclass
class MockState:
    logged_in: bool = False
    page: str = "welcome"
    entries: List[MockEntry] = field(default_factory=list)
    login_attempts: int = 0
    pending: MockEntry = field(default_factory=MockEntry)
    favorites_on: bool = True
    wizard_items: List[str] = field(default_factory=list)


def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:'Microsoft YaHei',sans-serif;font-size:14px}}table{{border-collapse:collapse}}
td,th{{border:1px solid #bbb;padding:3px 8px}}</style></head><body>{body}</body></html>"""


class MockWimServer:
    """在本机起一个假的理光后台。"""

    def __init__(self, port: int = 0) -> None:
        self.state = MockState()
        self.state.entries = [
            MockEntry("00001", "请用文件夹地址", "请用文件夹地址"),
            MockEntry("00002", "赵鹏 新", "赵鹏 新"),
            MockEntry("00007", "新扫描地址", "新扫描地址", "FTP", "21", "192.168.1.11", "/", "anonymous"),
        ]
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = port

    # ------------------------------------------------------------------ 生命周期
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args) -> None:  # 安静
                pass

            def do_GET(self) -> None:
                outer._handle(self, "GET")

            def do_POST(self) -> None:
                outer._handle(self, "POST")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    # ------------------------------------------------------------------ 路由
    def _handle(self, handler: BaseHTTPRequestHandler, method: str) -> None:
        parsed = urllib.parse.urlparse(handler.path)
        params: Dict[str, str] = dict(urllib.parse.parse_qsl(parsed.query))
        if method == "POST":
            length = int(handler.headers.get("Content-Length") or 0)
            body = handler.rfile.read(length).decode("utf-8", errors="replace") if length else ""
            params.update(dict(urllib.parse.parse_qsl(body)))

        path = parsed.path
        if path.endswith("mainFrame.cgi"):
            self._serve_frameset(handler)
            return
        if path.endswith("left.html"):
            self._send(handler, self._left_menu())
            return
        if path in ("/", "") or path.endswith("index.html"):
            self._serve_frameset(handler)
            return
        if path.endswith("content.cgi"):
            self._serve_content(handler, params)
            return
        self._send(handler, _page("404", "<h1>404</h1>"), status=404)

    def _send(self, handler: BaseHTTPRequestHandler, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    # ------------------------------------------------------------------ 页面
    def _serve_frameset(self, handler: BaseHTTPRequestHandler) -> None:
        body = (
            f"<html><head><meta charset='utf-8'><title>RICOH {MODEL} Web Image Monitor</title></head>"
            "<frameset cols='180,*'><frame src='/left.html' name='menu'>"
            "<frame src='/content.cgi?page=welcome' name='main'></frameset></html>"
        )
        self._send(handler, body)

    def _left_menu(self) -> str:
        return _page(
            "menu",
            """<div style="background:#c8102e;color:#fff;padding:8px;font-weight:bold">
            RICOH MP C4504 Web Image Monitor</div>
            <ul style="list-style:none;padding:6px">设备管理</ul>
            <div style="padding-left:14px">
              <div><a href="/content.cgi?page=list" target="main">通讯簿</a></div>
              <div><a href="/content.cgi?page=config" target="main">配置</a></div>
            </div>""",
        )

    def _serve_content(self, handler: BaseHTTPRequestHandler, params: Dict[str, str]) -> None:
        page = params.get("page", "welcome")
        state = self.state

        if page == "login":
            self._send(handler, self._login_page())
            return
        if page == "do_login":
            if (params.get("user") or "").strip() != ADMIN_USER or params.get("password") != ADMIN_PASSWORD:
                state.login_attempts += 1
                self._send(handler, self._login_page(error="登录名或密码不正确"))
                return
            state.logged_in = True
            self._send(handler, self._list_page())
            return
        if page == "welcome":
            self._send(
                handler,
                _page(
                    f"RICOH {MODEL} Web Image Monitor",
                    "<h2>RICOH MP C4504 Web Image Monitor</h2>"
                    "<div style='text-align:right'><a href='/content.cgi?page=login'>登录</a></div>"
                    "<p>状态良好</p>",
                ),
            )
            return
        if not state.logged_in:
            self._send(handler, self._login_page())
            return

        if page == "list":
            self._send(handler, self._list_page())
        elif page == "delete":
            selected = [s for s in (params.get("sel") or "").split(",") if s]
            if selected:
                state.entries = [e for e in state.entries if e.no not in selected]
            self._send(handler, self._list_page())
        elif page == "wizard_items":
            state.pending = MockEntry(no=params.get("no", ""))
            self._send(handler, self._wizard_items_page())
        elif page == "step1":
            state.wizard_items = [k for k in ("basic", "email", "folder") if params.get(k)]
            self._send(handler, self._step1_page())
        elif page == "step2":
            state.pending.no = params.get("no", "")
            state.pending.name = params.get("name", "")
            state.pending.key_display = params.get("key", "")
            self._send(handler, self._step2_page())
        elif page == "step3":
            p = state.pending
            p.protocol = "FTP" if params.get("protocol") == "ftp" else "SMB"
            p.port = params.get("port", "")
            p.server = params.get("server", "")
            p.path = params.get("path", "")
            p.username = params.get("user", "")
            p.password = params.get("password", "")
            self._send(handler, self._step3_page())
        elif page == "done":
            p = state.pending
            if not any(e.no == p.no for e in state.entries):
                state.entries.append(p)
            state.page = "list"
            self._send(handler, self._list_page(done=True))
        else:
            self._send(handler, _page("unknown", f"<h1>未知页面 {html.escape(page)}</h1>"))

    def _login_page(self, error: str = "") -> str:
        return _page(
            f"登录 - RICOH {MODEL} Web Image Monitor",
            f"""<h2>RICOH MP C4504 Web Image Monitor</h2>
            <div style="font-size:12px">请输入管理员密码</div>
            {"<p style='color:#c00'>" + html.escape(error) + "</p>" if error else ""}
            <form method="GET" action="/content.cgi">
              <input type="hidden" name="page" value="do_login">
              <table>
                <tr><td>登录用户名</td><td><input type="text" name="user" value="admin"></td></tr>
                <tr><td>登录密码</td><td><input type="password" name="password"></td></tr>
              </table>
              <p><input type="submit" value="登录"></p>
            </form>""",
        )

    def _list_page(self, done: bool = False) -> str:
        state = self.state
        def folder_of(e: MockEntry) -> str:
            return f"{e.server}:{e.path}" if e.server else "---"

        rows = "".join(
            f"<tr><td><input type='checkbox' name='entryIndex' value='{html.escape(e.no)}'></td>"
            f"<td>👤</td><td>{html.escape(e.no)}</td><td>{html.escape(e.name)}</td>"
            f"<td>---</td><td>2026/10/30 13:00</td><td>---</td>"
            f"<td>{html.escape(folder_of(e))}</td></tr>"
            for e in state.entries
        )
        return _page(
            f"地址列表 - RICOH {MODEL} Web Image Monitor",
            f"""<h2>RICOH MP C4504 Web Image Monitor</h2>
            {"<p style='color:#1a7f37'>✅ 已注册</p>" if done else ""}
            <h3>地址列表(显示全部项目)</h3>
            <p>简易输入 <a href="/content.cgi?page=list">返回</a></p>
            <p>
              <a href="/content.cgi?page=wizard_items">添加用户</a>
              <a href="#">更改</a>
              <a href="#" onclick="var s=[...document.querySelectorAll('input[name=entryIndex]:checked')].map(b=>b.value).join(',');location.href='/content.cgi?page=delete&sel='+s;return false;">删除</a>
            </p>
            <p>已选择<span id="selCount">0</span></p>
            <p>用户 : {len(state.entries)} / 2000 项目</p>
            <table><tr><th>类型</th><th>注册编号</th><th>名称</th><th>用户代码</th>
            <th>最近使用日期</th><th>电子邮件地址</th><th>文件夹</th></tr>{rows}</table>""",
        )

    def _wizard_items_page(self) -> str:
        return _page(
            f"更改注册 - RICOH {MODEL} Web Image Monitor",
            """<h2>更改注册</h2>
            <p>选择要注册的项目，然后按 [转到下一项]。</p>
            <form method="GET" action="/content.cgi">
              <input type="hidden" name="page" value="step1">
              <h4>用户信息</h4>
              <p><input type="checkbox" name="basic" checked> 基本信息 —— 基本信息例如可注册的注册编号或名称。</p>
              <h4>目的地</h4>
              <p><input type="checkbox" name="email"> 电子邮件目的地 —— 可注册电子邮件。</p>
              <p><input type="checkbox" name="folder"> 文件夹目的地 —— 关于可注册的网络文件夹的信息。</p>
              <p><input type="submit" value="转到下一项"></p>
            </form>""",
        )

    def _step1_page(self) -> str:
        return _page(
            f"Step1.基本信息 - RICOH {MODEL} Web Image Monitor",
            """<h2>Step1.基本信息</h2>
            <p>输入项目。输入到[按键显示]中的名称将显示在控制面板的目的地键上。</p>
            <form method="GET" action="/content.cgi">
              <input type="hidden" name="page" value="step2">
              <table>
                <tr><td>■ 注册编号(必须的)</td><td>: <input type="text" name="no" value=""></td></tr>
                <tr><td>■ 名称(必须的)</td><td>: <input type="text" name="name" value=""></td></tr>
                <tr><td>■ 按键显示</td><td>: <input type="text" name="key" value=""></td></tr>
                <tr><td>■ 标题1</td><td>: <select name="t1"><option>无</option></select></td></tr>
                <tr><td>■ 添加到常用</td><td>: <input type="radio" name="fav" checked> 开
                    <input type="radio" name="fav"> 关</td></tr>
              </table>
              <p><input type="submit" value="转到下一项"></p>
            </form>""",
        )

    def _step2_page(self) -> str:
        return _page(
            f"Step2.文件夹目的地 - RICOH {MODEL} Web Image Monitor",
            """<h2>Step2.文件夹目的地</h2>
            <p>输入文件夹信息。</p>
            <form method="GET" action="/content.cgi">
              <input type="hidden" name="page" value="step3">
              <table>
                <tr><td>■ 协议</td>
                    <td>: <input type="radio" name="protocol" value="smb"> SMB
                        <input type="radio" name="protocol" value="ftp"> FTP</td></tr>
                <tr><td>■ 端口号(必须的)</td><td>: <input type="text" name="port" value=""></td></tr>
                <tr><td>■ 服务器名称(必须的)</td><td>: <input type="text" name="server" value=""></td></tr>
                <tr><td>■ 路径(必须的)</td><td>: <input type="text" name="path" value=""></td></tr>
                <tr><td>■ 登录用户名称</td><td>: <input type="text" name="user" value=""></td></tr>
                <tr><td>■ 更改登录密码</td>
                    <td>: <input type="radio" name="chpwd" value="off" checked> 关
                        <input type="radio" name="chpwd" value="on"> 开</td></tr>
                <tr><td>■ 登录密码</td><td>: <input type="password" name="password" value=""></td></tr>
                <tr><td>■ 确认登录密码</td><td>: <input type="password" name="password2" value=""></td></tr>
              </table>
              <p><input type="submit" value="转到下一项"></p>
            </form>""",
        )

    def _step3_page(self) -> str:
        p = self.state.pending
        items = "、".join(self.state.wizard_items) or "（未选择）"
        return _page(
            f"Step3.确认注册 - RICOH {MODEL} Web Image Monitor",
            f"""<h2>Step3.确认注册</h2>
            <p>检查项目，然后按[确定]。</p>
            <p>已选项目：{html.escape(items)}</p>
            <table>
              <tr><td>■ 注册编号</td><td>: {html.escape(p.no)}</td></tr>
              <tr><td>■ 名称</td><td>: {html.escape(p.name)}</td></tr>
              <tr><td>■ 按键显示</td><td>: {html.escape(p.key_display)}</td></tr>
              <tr><td>■ 协议</td><td>: {html.escape(p.protocol)}</td></tr>
              <tr><td>■ 端口号</td><td>: {html.escape(p.port)}</td></tr>
              <tr><td>■ 服务器名称</td><td>: {html.escape(p.server)}</td></tr>
              <tr><td>■ 路径</td><td>: {html.escape(p.path)}</td></tr>
              <tr><td>■ 登录用户名称</td><td>: {html.escape(p.username)}</td></tr>
            </table>
            <form method="GET" action="/content.cgi">
              <input type="hidden" name="page" value="done">
              <p><input type="submit" value="确定">　<a href="/content.cgi?page=list">取消</a></p>
            </form>""",
        )

    # ------------------------------------------------------------------ 断言助手
    def find_entry(self, name: str) -> Optional[MockEntry]:
        return next((e for e in self.state.entries if e.name == name), None)

    @property
    def entry_count(self) -> int:
        return len(self.state.entries)


if __name__ == "__main__":
    server = MockWimServer(port=18888)
    server.start()
    print(f"假理光后台已启动: {server.base_url}{'/web/guest/cn/websys/webArch/mainFrame.cgi'}")
    print("管理员密码: secret")
    try:
        while True:
            pass
    except KeyboardInterrupt:
        server.stop()
