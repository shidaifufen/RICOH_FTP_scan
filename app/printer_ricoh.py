"""理光（RICOH）Web Image Monitor 通讯簿自动注册。

对应教程第 4 步：打开打印机后台 → 管理员登录 → 设备管理/通讯簿 → 简易输入/添加用户
→ 更改注册（基本信息 + 文件夹目的地）→ Step1 基本信息 → Step2 文件夹目的地
→ Step3 确认注册 → 确定。

实现方式：用 CDP 驱动本机 Edge，走"人点界面"的同一条路 ——
不猜理光 CGI 的表单字段和隐藏令牌（那些随固件版本变化），
只认界面上稳定的中文元素名（“添加用户”“转到下一项”“文件夹目的地”）。

每个步骤失败都会落盘截图 + 各 frame 的 HTML，现场跑一次就能拿到排障证据。
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import requests

from .cdp import CdpBrowser, CdpError

WIM_PATH = "/web/guest/cn/websys/webArch/mainFrame.cgi"
DEFAULT_STEP_WAIT_MS = 1200

NEXT_TEXTS = ["转到下一项", "转至下一项", "下一项", "下一步", "Next"]
OK_TEXTS = ["确定", "确认注册", "确认", "完成", "OK", "Apply", "Register"]
EXCLUDE_BUTTONS = ["取消", "返回", "删除", "退出", "Cancel", "Back", "Delete", "Reset"]


class PrinterError(RuntimeError):
    """打印机侧任何可解释的失败。"""


@dataclass
class StepRecord:
    name: str
    ok: bool
    detail: str = ""
    when: float = field(default_factory=time.time)


@dataclass
class RegistrationResult:
    ok: bool
    message: str
    entry_no: str = ""
    steps: List[StepRecord] = field(default_factory=list)
    debug_dir: str = ""

    def summary(self) -> str:
        return "\n".join(
            f"{'✅' if s.ok else '❌'} {s.name}{('：' + s.detail) if s.detail else ''}"
            for s in self.steps
        )


# --------------------------------------------------------------------------- 连通性
def probe_printer(host: str, timeout: float = 4.0) -> Tuple[bool, str]:
    """不开浏览器先探一下打印机后台在不在，报错信息要能指导现场排查。"""
    host = (host or '').strip()
    if not host:
        return False, '打印机地址为空'
    if host.startswith(('http://', 'https://')):
        host = re.sub(r'^https?://', '', host).split('/')[0]

    # 打印机是内网 IP，禁用系统代理，否则公司代理会把请求吞掉（表现为莫名超时）
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.get(
            f'http://{host}{WIM_PATH}',
            timeout=timeout,
            allow_redirects=True,
            proxies={'http': None, 'https': None},
        )
        text = resp.text or ''
        if 'Web Image Monitor' not in text:
            return False, (
                f'{host} 有响应（HTTP {resp.status_code}），但页面不像理光 Web Image Monitor，'
                '请确认打印机地址是否正确'
            )

        model = ''
        direct = re.search(r'RICOH\s+([A-Za-z0-9 \-]+?)\s*Web Image Monitor', text)
        if direct:
            model = f'RICOH {direct.group(1).strip()}'
        else:
            # 首页是 frameset，正文里没有型号；型号在子 frame（topPage.cgi）里，补一次请求
            try:
                sub = session.get(
                    f'http://{host}/web/guest/cn/websys/webArch/topPage.cgi',
                    timeout=timeout,
                    proxies={'http': None, 'https': None},
                )
                sub_match = re.search(r'RICOH\s+([A-Za-z0-9 \-]+)', sub.text or '')
                if sub_match:
                    model = f'RICOH {sub_match.group(1).strip()}'
            except requests.exceptions.RequestException:
                pass
            if not model:
                title_match = re.search(r'<title>([^<]*)</title>', text, re.I)
                title = (title_match.group(1) if title_match else '').strip()
                name = re.sub(r'\s*-\s*Web Image Monitor\s*$', '', title, flags=re.I).strip()
                model = f'Web Image Monitor（{name}）' if name else 'Web Image Monitor'

        return True, f'已连接：{model}（HTTP {resp.status_code}）'
    except requests.exceptions.ConnectTimeout:
        return False, f'连接 {host} 超时：网络不通 / 打印机未开机 / 不在同一网段'
    except requests.exceptions.ConnectionError:
        return False, f'无法连接 {host}：网络不通或被防火墙拦截'
    except requests.exceptions.RequestException as exc:
        return False, f'访问 {host} 失败：{exc}'
    finally:
        session.close()


# --------------------------------------------------------------------------- 主流程
class RicohAddressBookRegistrar:
    """把"扫描到 FTP 文件夹"的目的地自动写进打印机通讯簿。"""

    def __init__(
        self,
        host: str,
        admin_user: str = "admin",
        admin_password: str = "",
        ftp_host: str = "",
        ftp_port: int = 21,
        ftp_user: str = "anonymous",
        ftp_password: str = "",
        ftp_path: str = "/",
        headless: bool = True,
        step_wait_ms: int = DEFAULT_STEP_WAIT_MS,
        log: Optional[Callable[[str], None]] = None,
        debug_dir: Optional[str] = None,
        add_to_favorites: bool = True,
        on_browser: Optional[Callable[[CdpBrowser], None]] = None,
        browser: Optional["CdpBrowser"] = None,
        merge_by_ip: bool = True,
    ) -> None:
        self.host = (host or "").strip().replace("http://", "").replace("https://", "").strip("/")
        self.admin_user = (admin_user or "admin").strip() or "admin"
        self.admin_password = admin_password or ""
        self.ftp_host = (ftp_host or "").strip()
        self.ftp_port = int(ftp_port or 21)
        self.ftp_user = (ftp_user or "anonymous").strip() or "anonymous"
        self.ftp_password = ftp_password or ""
        self.ftp_path = (ftp_path or "/").strip() or "/"
        self.headless = headless
        self.step_wait_ms = max(200, int(step_wait_ms))
        self.log = log or (lambda msg: None)
        self.debug_dir = debug_dir or os.path.join(os.getcwd(), "logs", "debug")
        self.add_to_favorites = add_to_favorites
        self.on_browser = on_browser
        # 同一个 IP 视为同一用户（默认开）。关掉之后就只按名字合并。
        self.merge_by_ip = merge_by_ip
        # 复用调用方给的浏览器（一次会话只启动一次浏览器，避免反复触发失败登录）
        self.browser: Optional[CdpBrowser] = browser
        self._own_browser = browser is None

        self.steps: List[StepRecord] = []
        self._debug_seq = 0
        self._existing: List[Tuple[str, str]] = []
        # 注册编号 -> 打印机里记录的"文件夹"文本（用来按 IP 认人）
        self._entry_folders: Dict[str, str] = {}
        self._all_entry_numbers: List[int] = []

    # ------------------------------------------------------------------ 对外
    def register(self, name: str, entry_no: str = "") -> RegistrationResult:
        name = (name or "").strip()
        if not name:
            return RegistrationResult(False, "目的地名字不能为空")
        if not self.ftp_host:
            return RegistrationResult(False, "本机 FTP 地址未知，无法写入打印机")

        ok, detail = probe_printer(self.host)
        self._record("连接打印机后台", ok, detail)
        if not ok:
            return self._fail(detail)

        os.makedirs(self.debug_dir, exist_ok=True)
        self._debug_seq = 0

        browser = self.browser
        if browser is None or not browser.is_alive():
            browser = CdpBrowser(headless=self.headless, log=self.log)
            try:
                browser.start()
            except CdpError as exc:
                return self._fail(f"无法启动浏览器：{exc}")
            self._own_browser = True
        else:
            self.log("[打印机] 复用已启动的浏览器（不再新开进程）")
        self.browser = browser
        if self.on_browser:
            try:
                self.on_browser(browser)
            except Exception:
                pass

        try:
            return self._run_flow(name, entry_no)
        except PrinterError as exc:
            self._dump("failed")
            return self._fail(str(exc))
        except CdpError as exc:
            self._dump("browser-error")
            return self._fail(f"浏览器自动化失败：{exc}")
        except Exception as exc:  # 兜底：任何意外都留证据
            self._dump("unexpected")
            return self._fail(f"自动注册异常：{exc}")
        finally:
            # 只关自己启动的浏览器；外部传进来的由调用方统一关闭
            if self._own_browser and browser is not None:
                try:
                    self.logout()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
                self.browser = None

    # ------------------------------------------------------------------ 流程
    def _run_flow(self, name: str, entry_no: str) -> RegistrationResult:
        self._goto_main()
        self._record("打开 Web Image Monitor", True, self.host)

        self._login()
        self._record("管理员登录", True, self.admin_user)

        self._open_address_book()
        self._record("进入通讯簿", True)

        self._existing = self._read_address_list()
        self._record(
            "读取现有地址列表",
            True,
            f"共 {len(self._existing)} 条" if self._existing else "未读到（可忽略）",
        )

        # 同名、或同一个 FTP 服务器 IP（= 同一台电脑）都视为"同一个用户"：
        # 做法是【先新增，成功后再删旧】，这样中途失败也不会把原有条目弄丢。
        existing = self._find_existing(name)
        if existing is not None:
            old_no, reason = existing
            self._record("发现同一条目", True, f"{reason}，注册编号 {old_no}，稍后更新")
            entry_no = ""  # 更新时重新分配编号
        if not entry_no:
            entry_no = self._next_entry_no()
        entry_no = self._normalize_entry_no(entry_no)
        self._record("分配注册编号", True, entry_no)

        self._open_add_user_wizard()
        self._record("打开「添加用户」向导", True)

        self._choose_registration_items()
        self._record("勾选「基本信息 + 文件夹目的地」", True)

        self._fill_basic_info(name, entry_no)
        self._record("填写基本信息", True, f"名称/按键显示 = {name}")

        self._fill_folder_destination()
        self._record(
            "填写文件夹目的地",
            True,
            f"FTP://{self.ftp_host}:{self.ftp_port}{self.ftp_path}，用户 {self.ftp_user}",
        )

        self._confirm_registration()
        self._record("确认注册", True)

        if not self._verify(name, entry_no):
            raise PrinterError(
                "点击「确定」后没有在地址列表里找到新条目，注册可能没有生效。"
                "请查看 logs/debug 下的截图，确认打印机是否弹出了格式校验提示。"
            )
        self._record("校验注册结果", True, f"地址列表已出现「{name}」")

        updated_from = ""
        if existing is not None:
            old_no, reason = existing
            deleted = self._delete_entry(old_no)
            self._record(
                "清理旧条目", deleted,
                f"注册编号 {old_no}（{reason}）" + ("已删除" if deleted else "删除失败，请手动删"),
            )
            updated_from = old_no

        if updated_from:
            message = (
                f"已更新：打印机面板「扫描」里「{name}」仍然可用（原注册编号 {updated_from} 已由新条目替代），"
                "扫描文件会直接进本机文件夹。"
            )
        else:
            message = (
                f"注册完成：打印机面板「扫描」里能看到「{name}」，"
                "扫描文件会直接进本机文件夹。"
            )

        return RegistrationResult(
            ok=True,
            message=message,
            entry_no=entry_no,
            steps=self.steps,
            debug_dir=self.debug_dir,
        )

    # ------------------------------------------------------------------ 步骤
    def _goto_main(self) -> None:
        url = f"http://{self.host}{WIM_PATH}"
        self.log(f"[打印机] 打开 {url}")
        self._browser().navigate(url, wait_ready=True, ready_timeout=30)

    SESSION_LIMIT_HINTS = ("超过最大允许限制", "超过上限", "最大允许数")

    def _check_session_limit(self) -> None:
        """理光后台并发会话有限（默认几条），占满后进通讯簿会报错。"""
        text = self._browser().page_text()
        if any(hint in text for hint in self.SESSION_LIMIT_HINTS):
            raise PrinterError(
                "打印机后台会话数已满（提示：进入服务器的用户数超过最大允许限制）。"
                "等 1-2 分钟让它自动释放，或重启一次打印机；"
                "本程序已改为注册完自动注销，正常使用不会占满。"
            )

    def logout(self) -> None:
        """退出打印机后台管理员会话（释放会话名额）。"""
        browser = self.browser
        if browser is None or not browser.is_alive():
            return
        try:
            if self._click_text(["注销", "登出", "Logout", "Log out"], optional=True):
                self._wait(1.0)
                self.log("[打印机] 已注销后台会话")
        except Exception:
            pass

    def _is_logged_in(self) -> bool:
        text = self._browser().page_text()
        return ("注销" in text or "Log out" in text or "Logout" in text) and not self._has_password_field()

    def _login(self) -> None:
        if self._is_logged_in():
            self.log("[打印机] 已经是登录状态，复用当前会话")
            return
        if not self._has_password_field():
            if not self._click_text(["登录", "Login"]):
                raise PrinterError("页面上找不到「登录」按钮，打印机后台界面可能和预期不同")
            self._wait(1.5)

        if not self._has_password_field():
            raise PrinterError("点了「登录」但没出现密码输入框")

        self._fill_login(self.admin_user, self.admin_password)
        if not self._click_text(["登录", "Login", "确定", "OK"], exclude_buttons=False):
            raise PrinterError("登录页上找不到「登录/确定」按钮")
        self._wait(2.5)

        if self._has_password_field():
            self._dump("login-failed")
            raise PrinterError(
                "管理员登录失败：密码不对或账号受限。"
                "请先用同一浏览器手工登录核对一次管理员密码。"
            )

    def _open_address_book(self) -> None:
        # 有些固件的「通讯簿」直接可见，有些要先展开「设备管理」，两种都试。
        if not self._open_address_book_once():
            # 删除条目后页面可能还没回到主页/或会话被踢回登录页，重来一次
            self.log("[打印机] 进通讯簿失败，重新打开后台再试一次")
            self._goto_main()
            self._login()
            if not self._open_address_book_once():
                raise PrinterError("打印机后台里找不到「设备管理」或「通讯簿」菜单")
    def _open_address_book_once(self) -> bool:
        if not self._click_text(["通讯簿"], optional=True):
            if not self._click_text(["设备管理"]):
                return False
            self._wait(1.0)
            if not self._click_text(["通讯簿"]):
                return False
        self._wait(2.0)
        self._check_session_limit()
        self._click_text(["简易输入"], optional=True)
        self._wait(0.5)
        self._check_session_limit()
        return True

    def _collect_address_rows(self) -> List[List[str]]:
        rows_all = self._browser().collect_all(
            """() => {
                const out = [];
                for (const tr of document.querySelectorAll('tr')) {
                    const cells = [...(tr.cells || [])].map(c => (c.innerText || '').trim());
                    if (cells.length >= 3) out.push(cells);
                }
                return out.length ? out : null;
            }"""
        )
        rows: List[List[str]] = []
        for chunk in rows_all:
            if isinstance(chunk, list):
                rows.extend([r for r in chunk if isinstance(r, list)])
        return rows

    @staticmethod
    def _scan_entry_folders(rows: List[List[str]]) -> Dict[str, str]:
        """注册编号 -> 该行的"文件夹"文本（形如 192.168.1.10:/ 或 192.168.1.20:\\shishi）"""
        folders: Dict[str, str] = {}
        for cells in rows:
            number = None
            for cell in cells:
                text = str(cell or '').strip()
                if re.fullmatch(r'\d{5}', text):
                    number = text
                    break
            if not number:
                continue
            tail = [str(c).strip() for c in cells if str(c).strip() and str(c).strip() != '---']
            if tail:
                folders[number] = tail[-1]
        return folders

    @staticmethod
    def _parse_address_rows(rows: List[List[str]]) -> List[Tuple[str, str]]:
        result: List[Tuple[str, str]] = []
        seen = set()
        for cells in rows:
            for idx in range(min(3, len(cells) - 1)):
                cell = str(cells[idx] or "")
                if re.fullmatch(r"\d{5}", cell) and cell not in seen:
                    name = str(cells[idx + 1] if idx + 1 < len(cells) else "").strip()
                    if name and not re.fullmatch(r"[\d\-]+", name):
                        seen.add(cell)
                        result.append((cell, name))
                    break
        return result

    def _expected_entry_count(self) -> Optional[int]:
        """从页面上的「用户 : 49 / 2000 项目」里读出应有条数。"""
        match = re.search(r"用户\s*[:：]\s*(\d+)\s*/\s*(\d+)", self._browser().page_text())
        return int(match.group(1)) if match else None

    def _read_address_list(self, timeout: float = 12.0) -> List[Tuple[str, str]]:
        """读地址列表。

        真机的地址列表是**异步加载**的：页面先出现"正在读取…"，表格行是之后
        XHR 填进来的，一次读往往只读到一部分。所以这里轮询到"行数连续几次不变"
        为止；同时把所有 5 位注册编号都扫出来（有些条目没有名称，靠名称解析会漏），
        用它们算下一个编号，避免编号撞车。
        """
        deadline = time.time() + timeout
        entries: List[Tuple[str, str]] = []
        previous = -1
        stable = 0
        while time.time() < deadline:
            rows = self._collect_address_rows()
            entries = self._parse_address_rows(rows)
            self._all_entry_numbers = self._scan_entry_numbers(rows)
            self._entry_folders = self._scan_entry_folders(rows)
            expected = self._expected_entry_count()
            if expected == 0 and not entries:
                return entries
            if len(entries) == previous and entries:
                stable += 1
                if stable >= 3:
                    return entries
            else:
                stable = 0
            previous = len(entries)
            time.sleep(0.7)
            self._browser().pump()
        self.log(f"[打印机] 地址列表读到 {len(entries)} 条（未等到完全稳定，继续）")
        return entries

    @staticmethod
    def _scan_entry_numbers(rows: List[List[str]]) -> List[int]:
        numbers: List[int] = []
        for cells in rows:
            for cell in cells:
                text = str(cell or "").strip()
                if re.fullmatch(r"\d{5}", text):
                    value = int(text)
                    if 1 <= value <= 99999 and value not in numbers:
                        numbers.append(value)
        return numbers

    def _find_existing(self, name: str) -> Optional[Tuple[str, str]]:
        """找出"同一个用户"的已有条目，返回 (注册编号, 命中原因)。"""
        for no, entry_name in self._existing:
            if entry_name == name:
                return no, "同名"
        if self.merge_by_ip and self.ftp_host:
            for no, entry_name in self._existing:
                folder = self._entry_folders.get(no, "")
                if folder and self.ftp_host in folder:
                    return no, f"同一 IP（{entry_name or no}）"
        return None

    def _delete_entry(self, entry_no: str) -> bool:
        """按注册编号删除一条通讯簿记录（勾选行 -> 删除 -> 自动确认）。"""
        if not entry_no:
            return False
        picked = self._browser().evaluate_all(
            """(no) => {
                for (const tr of document.querySelectorAll('tr')) {
                    const cells = [...(tr.cells || [])].map(c => (c.innerText || '').trim());
                    if (!cells.includes(no)) continue;
                    const box = tr.querySelector('input[type="checkbox"]');
                    if (!box) return 'no-checkbox';
                    if (!box.checked) box.click();
                    return 'checked';
                }
                return null;
            }""",
            entry_no,
        )
        if not picked:
            self.log(f"[打印机] 删除失败：地址列表里没找到注册编号 {entry_no}")
            return False
        self._wait(0.8)
        if not self._click_text(["删除", "删除(Delete)"], optional=True):
            self.log("[打印机] 删除失败：找不到「删除」按钮")
            return False
        self._wait(2.0)
        # 有的固件弹 JS confirm（已自动确认），有的出现确认按钮
        self._click_text(["确定", "确认", "是", "OK"], optional=True)
        self._wait(1.5)
        entries = self._read_address_list()
        self._existing = entries
        return all(no != entry_no for no, _ in entries)

    def _next_entry_no(self) -> str:
        nums = [int(no) for no, _ in self._existing if no.isdigit()]
        # 名称解析可能漏掉个别条目，所以把扫到的全部 5 位编号也算进来取最大
        nums.extend(self._all_entry_numbers)
        nxt = (max(nums) + 1) if nums else 1
        if nxt > 99999:
            raise PrinterError("通讯簿注册编号已用满（00001-99999），请先清理旧条目")
        return f"{nxt:05d}"

    @staticmethod
    def _normalize_entry_no(value: str) -> str:
        value = (value or "").strip()
        digits = re.sub(r"\D", "", value)
        if not digits:
            digits = "1"
        return f"{int(digits):05d}"

    def _open_add_user_wizard(self) -> None:
        if not self._click_text(["添加用户"]):
            raise PrinterError("地址列表页找不到「添加用户」按钮")
        self._wait(2.0)

    def _choose_registration_items(self) -> None:
        self._wait(0.3)
        ok = self._check_by_label("文件夹目的地")
        if not ok:
            ok = self._check_checkbox_index(2)
        if not ok:
            raise PrinterError("「更改注册」页勾选「文件夹目的地」失败")
        self._wait(0.4)
        if not self._click_next():
            raise PrinterError("「更改注册」页找不到「转到下一项」按钮")

    def _fill_basic_info(self, name: str, entry_no: str) -> None:
        self._wait(0.3)
        filled = self._fill_text_inputs([entry_no, name, name], ["注册编号", "名称", "按键显示"])
        if filled < 3:
            raise PrinterError(
                f"Step1 基本信息只填上了 {filled}/3 个输入框（注册编号 / 名称 / 按键显示）"
            )
        if not self._click_next():
            raise PrinterError("Step1 基本信息页找不到「转到下一项」按钮")

    def _fill_folder_destination(self) -> None:
        self._wait(0.4)
        # 协议：SMB / FTP —— 点第 2 个单选按钮
        if not self._click_radio_index(1):
            raise PrinterError("Step2 页面上找不到协议单选按钮（SMB / FTP）")

        values = [str(self.ftp_port), self.ftp_host, self.ftp_path, self.ftp_user]
        labels = ["端口号", "服务器名称", "路径", "登录用户名称"]
        filled = self._fill_text_inputs(values, labels)
        if filled < 4:
            raise PrinterError(
                f"Step2 文件夹目的地只填上了 {filled}/4 个输入框"
                "（端口号 / 服务器名称 / 路径 / 登录用户名称）"
            )

        if self.ftp_password:
            self._click_radio_index(3)  # 更改登录密码：开
            self._wait(0.4)
            self._fill_password_inputs(self.ftp_password)

        if not self._click_next():
            raise PrinterError("Step2 文件夹目的地页找不到「转到下一项」按钮")

    def _confirm_registration(self) -> None:
        self._wait(0.5)
        if not self._click_text(OK_TEXTS, exclude_buttons=False, fallback=False):
            if not self._click_button_fallback():
                raise PrinterError("Step3 确认页找不到「确定」按钮")
        self._wait(3.0)

    def _verify(self, name: str, entry_no: str) -> bool:
        for _ in range(6):
            text = self._browser().page_text()
            if name and name in text:
                return True
            if entry_no and entry_no in text and "注册编号" in text:
                return True
            self._wait(1.0)
            try:
                self._browser().navigate(
                    f"http://{self.host}{WIM_PATH}", wait_ready=True, ready_timeout=20
                )
                self._open_address_book()
            except Exception:
                return False
        return False

    # ------------------------------------------------------------------ JS 助手
    def _browser(self) -> CdpBrowser:
        if self.browser is None:
            raise PrinterError("浏览器未启动")
        return self.browser

    def _has_password_field(self) -> bool:
        return bool(
            self._browser().evaluate_all(
                """() => {
                    const ps = [...document.querySelectorAll('input[type="password"]')];
                    return ps.some(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    }) ? 1 : null;
                }"""
            )
        )

    def _click_text(
        self,
        texts: Sequence[str],
        optional: bool = False,
        exclude_buttons: bool = True,
        fallback: bool = True,
    ) -> bool:
        for text in texts:
            hit = self._browser().evaluate_all(
                """(text) => {
                    const norm = s => (s || '').replace(/[\\s\\u00a0]+/g, '');
                    const want = norm(text);
                    if (!want) return null;
                    const CLICKABLE = ['A', 'BUTTON', 'INPUT', 'AREA', 'LABEL', 'IMG', 'SELECT'];
                    // 遍历所有元素：理光后台的菜单项可能是 <a>、<td>、<ul>、<span> 各种形态，
                    // 用标签白名单会漏（实测 <ul>设备管理</ul> 就被漏掉过）。
                    const SKIP = ['HTML', 'HEAD', 'BODY', 'SCRIPT', 'STYLE', 'FRAMESET',
                                  'FRAME', 'NOSCRIPT', 'META', 'TITLE', 'LINK', 'IFRAME'];
                    const nodes = [...document.querySelectorAll('*')]
                        .filter(el => !SKIP.includes(el.tagName));
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const attrLabel = el => norm(el.value)
                        || norm(el.getAttribute && el.getAttribute('alt'))
                        || norm(el.getAttribute && el.getAttribute('title'));
                    const textOf = el => norm(el.innerText || el.textContent);

                    // 1) 表单按钮 / 图片按钮：按 value / alt / title 精确匹配
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        if (attrLabel(el) === want) { el.click(); return 'button:' + el.tagName; }
                    }
                    // 2) 精确文字：优先"可点击标签 + 后代节点最少"。
                    //    必须点最内层 —— 点到包住 <a> 的 <div>/<td> 上看着成功、实际没反应。
                    const exact = nodes.filter(el => visible(el) && textOf(el) === want);
                    exact.sort((a, b) => {
                        const score = el => (CLICKABLE.includes(el.tagName) ? 0 : 1000)
                            + el.querySelectorAll('*').length;
                        return score(a) - score(b);
                    });
                    if (exact.length) { exact[0].click(); return 'text:' + exact[0].tagName; }
                    // 3) 包含匹配：可点击标签优先，其次文本最短
                    let best = null, bestScore = 1e9;
                    for (const el of nodes) {
                        if (!visible(el)) continue;
                        const t = textOf(el);
                        if (!t || !t.includes(want)) continue;
                        const score = t.length + (CLICKABLE.includes(el.tagName) ? 0 : 500);
                        if (score < bestScore) { best = el; bestScore = score; }
                    }
                    if (best) { best.click(); return 'contains:' + best.tagName; }
                    return null;
                }""",
                text,
            )
            if hit:
                self.log(f"[打印机] 点击「{text}」（命中 {hit}）")
                self._wait(1.0)
                return True

        if fallback and not optional:
            if self._click_button_fallback():
                return True
        if optional:
            return False
        self._dump(f"click-miss-{re.sub(r'[^A-Za-z0-9]', '', texts[0])[:16]}")
        return False

    def _click_next(self) -> bool:
        return self._click_text(NEXT_TEXTS)

    def _click_button_fallback(self) -> bool:
        """找不到文字时，点最后一个既不是取消/返回/删除的按钮。"""
        hit = self._browser().evaluate_all(
            """(excludes) => {
                const norm = s => (s || '').replace(/[\\s\\u00a0]+/g, '');
                const ex = (excludes || []).map(norm).filter(Boolean);
                const els = [...document.querySelectorAll(
                    'input[type="submit"],input[type="button"],input[type="image"],button'
                )].filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                for (const el of els.reverse()) {
                    const label = norm(el.value)
                        || norm(el.getAttribute('alt'))
                        || norm(el.getAttribute('title'))
                        || norm(el.innerText);
                    if (ex.some(e => e && label.includes(e))) continue;
                    el.click();
                    return 'fallback:' + (label || el.tagName);
                }
                return null;
            }""",
            EXCLUDE_BUTTONS,
        )
        if hit:
            self.log(f"[打印机] 未匹配到文字，使用兜底按钮（{hit}）")
            self._wait(1.2)
            return True
        return False

    def _fill_login(self, user: str, password: str) -> None:
        ok = self._browser().evaluate_all(
            """([user, password]) => {
                const visible = el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0 && !el.disabled;
                };
                const pwd = [...document.querySelectorAll('input[type="password"]')].filter(visible)[0];
                if (!pwd) return null;
                pwd.focus(); pwd.value = password;
                pwd.dispatchEvent(new Event('input', {bubbles: true}));
                pwd.dispatchEvent(new Event('change', {bubbles: true}));
                const texts = [...document.querySelectorAll(
                    'input[type="text"], input:not([type])'
                )].filter(visible);
                if (texts.length) {
                    const el = texts[0];
                    el.focus(); el.value = user;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }
                return 'filled';
            }""",
            [user, password],
        )
        if not ok:
            raise PrinterError("登录页找不到密码输入框")

    def _check_by_label(self, label: str) -> bool:
        return bool(
            self._browser().evaluate_all(
                """(label) => {
                    const norm = s => (s || '').replace(/[\\s\\u00a0]+/g, '');
                    const want = norm(label);
                    for (const box of document.querySelectorAll('input[type="checkbox"]')) {
                        let node = box, scope = '';
                        for (let i = 0; i < 3 && node; i++) {
                            scope += ' ' + (node.innerText || node.textContent || '');
                            node = node.parentElement;
                        }
                        if (norm(scope).includes(want)) {
                            if (!box.checked) box.click();
                            return 'by-label';
                        }
                    }
                    return null;
                }""",
                label,
            )
        )

    def _check_checkbox_index(self, index: int) -> bool:
        return bool(
            self._browser().evaluate_all(
                """(index) => {
                    const boxes = [...document.querySelectorAll('input[type="checkbox"]')]
                        .filter(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        });
                    if (boxes.length <= index) return null;
                    if (!boxes[index].checked) boxes[index].click();
                    return 'by-index';
                }""",
                index,
            )
        )

    def _click_radio_index(self, index: int) -> bool:
        return bool(
            self._browser().evaluate_all(
                """(index) => {
                    const radios = [...document.querySelectorAll('input[type="radio"]')]
                        .filter(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0;
                        });
                    if (radios.length <= index) return null;
                    radios[index].click();
                    return 'by-index';
                }""",
                index,
            )
        )

    def _fill_text_inputs(self, values: Sequence[str], labels: Sequence[str]) -> int:
        filled = 0
        for position, (value, label) in enumerate(zip(values, labels)):
            ok = self._browser().evaluate_all(
                """([label, value]) => {
                    const norm = s => (s || '').replace(/[\\s\\u00a0:：]+/g, '');
                    const want = norm(label);
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && !el.disabled;
                    };
                    const boxes = [...document.querySelectorAll(
                        'input[type="text"], input:not([type]), textarea'
                    )].filter(visible);
                    for (const el of boxes) {
                        let node = el, scope = '';
                        for (let i = 0; i < 3 && node; i++) {
                            scope += ' ' + (node.innerText || node.textContent || '');
                            node = node.parentElement;
                        }
                        if (norm(scope).includes(want)) {
                            el.focus(); el.value = value;
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            return 'by-label';
                        }
                    }
                    return null;
                }""",
                [label, value],
            )
            if ok:
                filled += 1
                continue
            ok = self._browser().evaluate_all(
                """([index, value]) => {
                    const visible = el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0 && !el.disabled;
                    };
                    const boxes = [...document.querySelectorAll(
                        'input[type="text"], input:not([type]), textarea'
                    )].filter(visible);
                    if (boxes.length <= index) return null;
                    const el = boxes[index];
                    el.focus(); el.value = value;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return 'by-index';
                }""",
                [position, value],
            )
            if ok:
                filled += 1
        return filled

    def _fill_password_inputs(self, password: str) -> None:
        self._browser().evaluate_all(
            """(password) => {
                const ps = [...document.querySelectorAll('input[type="password"]')].filter(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                for (const el of ps) {
                    el.focus(); el.value = password;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                }
                return ps.length ? 'ok' : null;
            }""",
            password,
        )

    # ------------------------------------------------------------------ 工具
    def _wait(self, factor: float = 1.0) -> None:
        """等一会儿，并把 CDP 事件泵一遍。

        页面跳转后新的执行上下文是通过事件通知的，只 sleep 不会刷新上下文，
        后面所有 evaluate 都会打在已销毁的 context 上（表现为"页面上找不到元素"）。
        """
        seconds = (self.step_wait_ms * factor) / 1000.0
        time.sleep(max(0.0, seconds))
        browser = self.browser
        if browser is not None:
            browser.pump()

    def _record(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append(StepRecord(name, ok, detail))
        self.log(f"[打印机] {'✓' if ok else '✗'} {name}{('：' + detail) if detail else ''}")

    def _fail(self, message: str) -> RegistrationResult:
        self._record("失败原因", False, message)
        return RegistrationResult(
            ok=False, message=message, steps=self.steps, debug_dir=self.debug_dir
        )

    def _dump(self, tag: str) -> None:
        """保存截图 + 各 frame HTML，供现场排障。"""
        browser = self.browser
        if browser is None:
            return
        try:
            self._debug_seq += 1
            os.makedirs(self.debug_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            base = os.path.join(self.debug_dir, f"{stamp}-{self._debug_seq:02d}-{tag}")
            browser.screenshot(base + ".png")
            with open(base + ".html", "w", encoding="utf-8", errors="ignore") as fh:
                fh.write(browser.html_dump())
            self.log(f"[打印机] 已保存现场快照：{base}.png / .html")
        except Exception:
            pass
