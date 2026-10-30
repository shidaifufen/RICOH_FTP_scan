"""扫描直连 · 一键注册（Windows GUI）。

对应《打印机扫描直连》教程：把"装 FTP 软件 + 打印机后台手工加通讯簿"这一套
压缩成"填一个名字 → 点确认"。

界面三分区：① 本机 FTP 接收服务 ② 打印机 ③ 一键注册。
所有耗时动作都在后台线程跑，日志通过队列回到界面，不卡窗口。
"""

from __future__ import annotations

import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import cdp, firewall, netinfo
from .cdp import CdpBrowser
from .config import AppConfig, default_scan_dir, load_config, save_config
from .ftp_server import DEFAULT_PASSIVE_RANGE, FtpScanServer, ReceivedFile, PYFTPDLIB_AVAILABLE
from .tray import TrayIcon
from .printer_ricoh import RicohAddressBookRegistrar, RegistrationResult, WIM_PATH, probe_printer

APP_TITLE = "扫描直连 · 一键注册"
VERSION = "1.0"

COLOR_IDLE = "#8c8c8c"
COLOR_OK = "#1a7f37"
COLOR_BAD = "#c62828"
COLOR_BUSY = "#b26a00"


def enable_hi_dpi() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class App:
    def __init__(self, root: tk.Tk, base_dir: str, enable_tray: bool = True) -> None:
        self.root = root
        self.base_dir = base_dir
        self.log_dir = os.path.join(base_dir, "logs")
        os.makedirs(self.log_dir, exist_ok=True)

        self.cfg: AppConfig = load_config(base_dir)
        if not self.cfg.save_dir:
            self.cfg.save_dir = default_scan_dir()

        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.tray_queue: "queue.Queue[str]" = queue.Queue()
        self.tray: Optional[TrayIcon] = None
        self.enable_tray = enable_tray
        self._exiting = False
        self.ftp: Optional[FtpScanServer] = None
        # 整个会话只保持一个浏览器：反复新开浏览器会让 Windows 产生失败登录事件，
        # 默认 10 次/10 分钟的锁定策略下足以把本机账户锁死（PIN 报 0xc0000234）。
        self._browser: Optional[CdpBrowser] = None
        self.busy = False
        self.last_result: Optional[RegistrationResult] = None

        self._closing = False
        self._after_id = None
        self._build_ui()
        self._apply_config()
        self._refresh_local_ip()
        self._schedule_log_drain()
        if self.enable_tray:
            self._init_tray()
        self._write_env_report()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.log(f"{APP_TITLE} v{VERSION} 已就绪")
        if not PYFTPDLIB_AVAILABLE:
            self.log("⚠ 缺少 pyftpdlib 依赖，请先运行 setup.bat（或把 vendor 目录一起拷过来）")

    # ================================================================ 界面
    def _build_ui(self) -> None:
        self.root.title(f"{APP_TITLE} v{VERSION}")
        self.root.geometry("900x760")
        self.root.minsize(820, 680)
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        default_font = ("Microsoft YaHei UI", 10)
        self.root.option_add("*Font", default_font)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Sub.TLabel", foreground="#555555")
        style.configure("Step.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Go.TButton", font=("Microsoft YaHei UI", 11, "bold"))

        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(side="left")
        ttk.Label(
            header,
            text="  填个名字 → 自动在打印机注册 → 面板直接扫描",
            style="Sub.TLabel",
        ).pack(side="left", padx=(6, 0), pady=(6, 0))

        # ---------------------------------------------------------- ① FTP
        row = 0
        ftp_box = ttk.LabelFrame(outer, text=" ① 本机接收服务（内置 FTP，不用装 Slyar） ", style="Step.TLabelframe", padding=10)
        ftp_box.pack(fill="x", pady=(12, 0))
        ftp_box.columnconfigure(1, weight=1)

        ttk.Label(ftp_box, text="保存目录").grid(row=row, column=0, sticky="w", pady=3)
        self.dir_var = tk.StringVar()
        ttk.Entry(ftp_box, textvariable=self.dir_var).grid(row=row, column=1, sticky="we", padx=6, pady=3)
        dir_btns = ttk.Frame(ftp_box)
        dir_btns.grid(row=row, column=2, sticky="e")
        ttk.Button(dir_btns, text="浏览…", width=8, command=self._pick_dir).pack(side="left")
        ttk.Button(dir_btns, text="打开", width=6, command=self._open_dir).pack(side="left", padx=4)

        row += 1
        opt = ttk.Frame(ftp_box)
        opt.grid(row=row, column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Label(opt, text="监听端口").pack(side="left")
        self.port_var = tk.StringVar(value="21")
        ttk.Entry(opt, textvariable=self.port_var, width=7).pack(side="left", padx=(4, 14))

        self.anon_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="允许匿名访问", variable=self.anon_var, command=self._on_anon_toggle).pack(side="left")

        ttk.Label(opt, text="账号").pack(side="left", padx=(14, 0))
        self.ftp_user_var = tk.StringVar(value="anonymous")
        self.user_entry = ttk.Entry(opt, textvariable=self.ftp_user_var, width=12)
        self.user_entry.pack(side="left", padx=4)
        ttk.Label(opt, text="密码").pack(side="left")
        self.ftp_pwd_var = tk.StringVar()
        self.pwd_entry = ttk.Entry(opt, textvariable=self.ftp_pwd_var, width=12, show="●")
        self.pwd_entry.pack(side="left", padx=4)

        row += 1
        ip_row = ttk.Frame(ftp_box)
        ip_row.grid(row=row, column=0, columnspan=3, sticky="we", pady=(8, 0))
        ttk.Label(ip_row, text="本机地址").pack(side="left")
        self.ip_var = tk.StringVar(value="检测中…")
        self._ip_combo = ttk.Combobox(ip_row, textvariable=self.ip_var, width=20, values=[])
        self._ip_combo.pack(side="left", padx=(4, 8))
        ttk.Button(ip_row, text="重新检测", width=9, command=self._refresh_local_ip).pack(side="left")
        self.ftp_state = ttk.Label(ip_row, text="● 未启动", foreground=COLOR_IDLE)
        self.ftp_state.pack(side="left", padx=(16, 0))
        ttk.Button(ip_row, text="启动服务", width=10, command=self._start_ftp).pack(side="right")
        ttk.Button(ip_row, text="停止服务", width=10, command=self._stop_ftp).pack(side="right", padx=(0, 6))

        row += 1
        self.ftp_hint = ttk.Label(
            ftp_box,
            text="",
            style="Sub.TLabel",
            wraplength=820,
            justify="left",
        )
        self.ftp_hint.grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # ---------------------------------------------------------- ② 打印机
        pr_box = ttk.LabelFrame(outer, text=" ② 打印机（RICOH Web Image Monitor） ", style="Step.TLabelframe", padding=10)
        pr_box.pack(fill="x", pady=(12, 0))
        pr_box.columnconfigure(1, weight=1)

        ttk.Label(pr_box, text="打印机地址").grid(row=0, column=0, sticky="w", pady=3)
        self.printer_var = tk.StringVar()
        ttk.Entry(pr_box, textvariable=self.printer_var, width=24).grid(row=0, column=1, sticky="w", padx=6, pady=3)
        ttk.Label(pr_box, text="（后台 http://地址/… 里的那个 IP）", style="Sub.TLabel").grid(row=0, column=2, sticky="w")

        cred = ttk.Frame(pr_box)
        cred.grid(row=1, column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Label(cred, text="管理员账号").pack(side="left")
        self.admin_user_var = tk.StringVar(value="admin")
        ttk.Entry(cred, textvariable=self.admin_user_var, width=14).pack(side="left", padx=4)
        ttk.Label(cred, text="密码").pack(side="left", padx=(10, 0))
        self.admin_pwd_var = tk.StringVar()
        ttk.Entry(cred, textvariable=self.admin_pwd_var, width=18, show="●").pack(side="left", padx=4)
        ttk.Button(cred, text="测试连接", width=10, command=self._test_printer).pack(side="left", padx=(12, 0))
        ttk.Button(cred, text="打开后台", width=10, command=self._open_wim).pack(side="left", padx=6)

        # ---------------------------------------------------------- ③ 一键注册
        go_box = ttk.LabelFrame(outer, text=" ③ 一键注册 ", style="Step.TLabelframe", padding=10)
        go_box.pack(fill="x", pady=(12, 0))
        go_box.columnconfigure(1, weight=1)

        ttk.Label(go_box, text="目的地名字").grid(row=0, column=0, sticky="w", pady=3)
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(go_box, textvariable=self.name_var, font=("Microsoft YaHei UI", 11))
        self.name_entry.grid(row=0, column=1, sticky="we", padx=6, pady=3)
        ttk.Label(
            go_box,
            text="← 这个名字会显示在打印机面板的扫描目的地里",
            style="Sub.TLabel",
        ).grid(row=0, column=2, sticky="w")

        opt2 = ttk.Frame(go_box)
        opt2.grid(row=1, column=0, columnspan=3, sticky="we", pady=(6, 0))
        self.entry_no_var = tk.StringVar(value="")
        ttk.Label(opt2, text="注册编号").pack(side="left")
        ttk.Entry(opt2, textvariable=self.entry_no_var, width=8).pack(side="left", padx=4)
        ttk.Label(opt2, text="（留空自动分配）", style="Sub.TLabel").pack(side="left")

        self.show_browser_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt2, text="处理时显示浏览器窗口（排障用）", variable=self.show_browser_var
        ).pack(side="left", padx=(18, 0))

        self.merge_by_ip_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            opt2, text="同名 / 同 IP 只保留一条（更新而不是新增）", variable=self.merge_by_ip_var
        ).pack(side="left", padx=(14, 0))

        action = ttk.Frame(go_box)
        action.grid(row=2, column=0, columnspan=3, sticky="we", pady=(12, 0))
        self.go_btn = ttk.Button(action, text="确认并注册", style="Go.TButton", command=self._on_go)
        self.go_btn.pack(side="left")
        self.verify_btn = ttk.Button(action, text="自检 FTP", width=10, command=self._self_check)
        self.verify_btn.pack(side="left", padx=8)
        ttk.Button(action, text="以管理员身份重启", width=16, command=self._relaunch_admin).pack(side="right")

        # ---------------------------------------------------------- 日志
        log_box = ttk.LabelFrame(outer, text=" 运行日志 ", padding=8)
        log_box.pack(fill="both", expand=True, pady=(12, 0))
        self.log_text = tk.Text(log_box, height=12, wrap="word", state="disabled", bg="#111418", fg="#d6d6d6",
                                insertbackground="#d6d6d6", relief="flat")
        scroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.status_var = tk.StringVar(value="就绪")
        self._status_label = ttk.Label(
            self.root, textvariable=self.status_var, anchor="w", padding=(14, 4)
        )
        self._status_label.pack(fill="x", side="bottom")

    # ================================================================ 配置
    def _apply_config(self) -> None:
        cfg = self.cfg
        self.dir_var.set(cfg.save_dir)
        self.port_var.set(str(cfg.port))
        self.anon_var.set(cfg.anonymous)
        self.ftp_user_var.set(cfg.ftp_user)
        self.ftp_pwd_var.set(cfg.ftp_password)
        self.printer_var.set(cfg.printer_host)
        self.admin_user_var.set(cfg.admin_user)
        self.admin_pwd_var.set(cfg.admin_password)
        self.name_var.set(cfg.last_name)
        self.show_browser_var.set(cfg.show_browser)
        self.merge_by_ip_var.set(cfg.merge_by_ip)
        self._on_anon_toggle()

    def _collect_config(self) -> AppConfig:
        cfg = self.cfg
        cfg.save_dir = self.dir_var.get().strip()
        cfg.port = int(self.port_var.get().strip() or 21)
        cfg.anonymous = bool(self.anon_var.get())
        cfg.ftp_user = self.ftp_user_var.get().strip() or "anonymous"
        cfg.ftp_password = self.ftp_pwd_var.get()
        cfg.printer_host = self.printer_var.get().strip()
        cfg.admin_user = self.admin_user_var.get().strip() or "admin"
        cfg.admin_password = self.admin_pwd_var.get()
        cfg.last_name = self.name_var.get().strip()
        cfg.show_browser = bool(self.show_browser_var.get())
        cfg.merge_by_ip = bool(self.merge_by_ip_var.get())
        return cfg

    # ================================================================ 小工具
    def log(self, message: str) -> None:
        self.log_queue.put(message)
        # 同时落一份文件：现场出问题时（尤其托盘/浏览器这类后台线程的错）
        # 只靠界面日志窗口是拿不到的
        try:
            path = os.path.join(self.log_dir, "app.log")
            if os.path.isfile(path) and os.path.getsize(path) > 2 * 1024 * 1024:
                os.replace(path, path + ".1")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    def _schedule_log_drain(self) -> None:
        self._after_id = self.root.after(120, self._drain_logs)

    def _init_tray(self) -> None:
        """创建通知区域图标；失败不影响主界面。"""
        try:
            self.tray = TrayIcon(
                tooltip="扫描直连 · FTP 未启动",
                menu_provider=self._tray_menu_items,
                on_menu=lambda action: self.tray_queue.put(action),
                on_show=lambda: self.tray_queue.put("show"),
                log=self.log,
            )
            if not self.tray.start():
                self.log(f"托盘图标不可用（{self.tray.last_error}），不影响使用")
                self.tray = None
        except Exception as exc:
            self.log(f"托盘初始化异常：{exc}")
            self.tray = None

    def _tray_menu_items(self):
        running = self.ftp is not None and self.ftp.running
        status = self.ftp.status_line() if running else "未启动"
        items = [
            ("show", "打开主界面", True),
            None,
            ("status", f"状态：{status}", False),
        ]
        if running:
            items.append(("copy", f"复制地址 {self.ftp.masquerade_ip}:{self.ftp.port}", True))
        items.append(None)
        items.append(("start", "启动 FTP 服务", (not running) and not self.busy))
        items.append(("stop", "停止 FTP 服务", running and not self.busy))
        items.append(("check", "检查状态", not self.busy))
        items.append(("open_dir", "打开保存目录", True))
        items.append(None)
        items.append(("quit", "退出", True))
        return items

    def _on_tray_action(self, action: str) -> None:
        if action == "show":
            self._show_window()
        elif action == "start":
            self._start_ftp()
        elif action == "stop":
            self._stop_ftp()
        elif action == "copy":
            self._copy_ftp_address()
        elif action == "check":
            self._check_status()
        elif action == "open_dir":
            self._open_dir()
        elif action == "quit":
            self._quit_app()

    def _show_window(self) -> None:
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except Exception:
            pass

    def _copy_ftp_address(self) -> None:
        if self.ftp is None or not self.ftp.running:
            return
        address = f"ftp://{self.ftp.masquerade_ip}:{self.ftp.port}/"
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(address)
            self.log(f"已复制到剪贴板：{address}")
        except Exception as exc:
            self.log(f"复制失败：{exc}")

    def _check_status(self) -> None:
        if self.ftp is None or not self.ftp.running:
            messagebox.showinfo(APP_TITLE, "FTP 服务未启动")
            return
        ok, detail = self.ftp.self_check()
        text = (
            f"FTP：{self.ftp.status_line()}\n"
            f"保存目录：{self.ftp.root_dir}\n"
            f"自检：{'通过' if ok else '失败'}（{detail}）\n"
            f"已收到文件：{len(self.ftp.received)} 个"
        )
        self.log(text.replace("\n", " | "))
        messagebox.showinfo(APP_TITLE, text)

    def _update_tray_tooltip(self) -> None:
        if self.tray is None:
            return
        if self.ftp is not None and self.ftp.running:
            self.tray.set_tooltip(
                f"扫描直连 · FTP 运行中 {self.ftp.masquerade_ip}:{self.ftp.port}"
            )
        else:
            self.tray.set_tooltip("扫描直连 · FTP 未启动")

    def _logout_printer_session(self) -> None:
        """退出前注销打印机后台会话：理光后台并发会话有限，占满会进不去通讯簿。"""
        if self._browser is None or not self._browser.is_alive():
            return
        try:
            cfg = self.cfg
            registrar = RicohAddressBookRegistrar(
                host=cfg.printer_host,
                admin_user=cfg.admin_user,
                admin_password=cfg.admin_password,
                ftp_host="",
                step_wait_ms=cfg.step_wait_ms,
                log=self.log,
                browser=self._browser,
            )
            registrar.logout()
        except Exception as exc:
            self.log(f"注销后台会话失败（忽略）：{exc}")

    def _quit_app(self) -> None:
        self._exiting = True
        self._on_close()

    def _drain_logs(self) -> None:
        if self._closing:
            return
        try:
            while True:
                action = self.tray_queue.get_nowait()
                self._on_tray_action(action)
        except queue.Empty:
            pass
        try:
            while True:
                msg = self.log_queue.get_nowait()
                stamp = time.strftime("%H:%M:%S")
                self.log_text.configure(state="normal")
                self.log_text.insert("end", f"[{stamp}] {msg}\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        if not self._closing:
            self._after_id = self.root.after(120, self._drain_logs)

    def _set_status(self, text: str, color: str = COLOR_IDLE) -> None:
        """更新底部状态栏文字，并让「● 已启动/未启动」始终反映 FTP 的真实状态。

        之前这里只改了标签颜色、没改文字，导致 FTP 明明启动成功，界面上仍写着
        「● 未启动」（还是绿色的，更容易让人误会）。
        """
        self.status_var.set(text)
        if getattr(self, "_status_label", None) is not None:
            try:
                self._status_label.configure(foreground=color)
            except tk.TclError:
                pass
        self._refresh_ftp_state()

    def _refresh_ftp_state(self) -> None:
        """按 FTP 服务真实运行情况刷新「● 已启动 / ● 未启动」标签。"""
        running = self.ftp is not None and self.ftp.running
        try:
            if running:
                self.ftp_state.configure(text="● 已启动", foreground=COLOR_OK)
            else:
                self.ftp_state.configure(text="● 未启动", foreground=COLOR_IDLE)
        except tk.TclError:
            pass

    def _on_anon_toggle(self) -> None:
        anonymous = self.anon_var.get()
        if anonymous:
            self.ftp_user_var.set("anonymous")
            self.ftp_pwd_var.set("")
        state = "disabled" if anonymous else "normal"
        self.user_entry.configure(state=state)
        self.pwd_entry.configure(state=state)

    def _pick_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.dir_var.get() or os.path.expanduser("~"))
        if chosen:
            self.dir_var.set(chosen)

    def _open_dir(self) -> None:
        path = self.dir_var.get().strip()
        if not path:
            return
        os.makedirs(path, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"打不开目录：{exc}")

    def _refresh_local_ip(self) -> None:
        printer_host = self.printer_var.get().strip()
        ip = netinfo.choose_local_ip(printer_host)
        addresses = [a.ip for a in netinfo.list_local_addresses()]
        self._ip_combo.configure(values=addresses or ([ip] if ip else []))
        if ip:
            self.ip_var.set(ip)
            self.log(f"本机地址（给打印机用）：{ip}")
        else:
            self.log("⚠ 没有检测到可用的本机 IP，请检查网络连接")

    def _pick_writable_dir(self, preferred: str) -> str:
        """选一个真的能写的保存目录。

        为什么需要：现场机器可能有安全软件/受控文件夹策略，往「桌面」写会被拒
        （日志里就是 `[WinError 5] 拒绝访问`）。这里按 桌面 -> 文档 -> 本地AppData
        依次降级，并在日志里说清楚最终落在哪。
        """
        candidates = [
            preferred,
            os.path.join(os.path.expanduser("~"), "Documents", "扫描"),
            os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "扫描直连", "扫描"),
            # 兜底：程序自己所在目录。受限容器（AI 助手 / 沙箱 / 某些安全策略）里
            # 用户目录可能整个不可写，但程序目录通常能写，至少保证功能可用。
            os.path.join(self.base_dir, "扫描件"),
        ]
        seen = set()
        for index, candidate in enumerate(candidates):
            if not candidate:
                continue
            normalized = os.path.abspath(candidate)
            if normalized in seen:
                continue
            seen.add(normalized)
            try:
                os.makedirs(normalized, exist_ok=True)
                probe = os.path.join(normalized, ".scanlink-write-test")
                with open(probe, "w", encoding="utf-8") as fh:
                    fh.write("ok")
                os.remove(probe)
            except OSError as exc:
                self.log(f"⚠ 保存目录不可用：{candidate}（{exc}）")
                continue
            if index > 0:
                if candidate == os.path.join(self.base_dir, "扫描件"):
                    self.log(
                        "⚠ 桌面/文档/LocalAppData 都不可写：当前应该是在受限环境里启动的"
                        "（AI 助手/沙箱/受限终端）。已临时把保存目录放到程序目录下的「扫描件」。"
                        " 想要托盘图标和「桌面\扫描」，请在 Windows 资源管理器里双击 启动.bat。"
                    )
                else:
                    self.log(f"⚠ 已自动改用可写目录：{normalized}")
                self.dir_var.set(normalized)
            return normalized
        raise OSError("没有找到可写的保存目录（桌面/文档/本地AppData 都被拒绝）")

    def _write_env_report(self) -> None:
        """启动时写一份环境自检报告：现场排障时直接发这个文件。"""
        lines = [f"扫描直连 v{VERSION} 环境自检  {time.strftime('%Y-%m-%d %H:%M:%S')}"]
        try:
            lines.append(f"exe/源码目录: {self.base_dir}")
            lines.append(f"打包运行: {bool(getattr(sys, 'frozen', False))}")
            lines.append(f"Python: {sys.version.split()[0]}")
            try:
                import getpass

                lines.append(f"当前用户: {getpass.getuser()}")
            except Exception:
                pass
            lines.append(f"管理员: {firewall.is_admin()}")
            try:
                session_id = ctypes.c_ulong()
                ctypes.windll.kernel32.ProcessIdToSessionId(
                    ctypes.windll.kernel32.GetCurrentProcessId(), ctypes.byref(session_id)
                )
                lines.append(f"会话ID: {session_id.value}")
            except Exception:
                pass
            candidates = [
                ("程序目录\扫描件", os.path.join(self.base_dir, "扫描件")),
                ("桌面\扫描", default_scan_dir()),
                ("文档\扫描", os.path.join(os.path.expanduser("~"), "Documents", "扫描")),
                ("LocalAppData", os.path.join(os.environ.get("LOCALAPPDATA", "?"), "扫描直连", "扫描")),
            ]
            for label, path in candidates:
                try:
                    os.makedirs(path, exist_ok=True)
                    probe = os.path.join(path, ".probe")
                    with open(probe, "w", encoding="utf-8") as fh:
                        fh.write("x")
                    os.remove(probe)
                    lines.append(f"写入测试 {label}: 可写 ({path})")
                except OSError as exc:
                    lines.append(f"写入测试 {label}: 拒绝 ({path}) -> {exc}")
            edge = cdp.find_browser()
            lines.append(f"浏览器: {edge or '未找到 Edge/Chrome'}")
            lines.append(f"托盘: {'可用' if (self.tray and self.tray.available) else '不可用'}")
            lines.append(f"打印机: {self.cfg.printer_host}")
            lines.append(f"保存目录配置: {self.cfg.save_dir}")
        except Exception as exc:  # 报告本身不能拖垮启动
            lines.append(f"自检异常: {exc!r}")
        try:
            path = os.path.join(self.log_dir, "env-report.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines))
            self.log("环境自检报告已写入 logs\\env-report.txt")
        except OSError:
            pass

    def _current_ip(self) -> str:
        return self.ip_var.get().strip()

    # ================================================================ FTP
    def _start_ftp(self, silent: bool = False) -> bool:
        try:
            save_dir = self._pick_writable_dir(self.dir_var.get().strip())
        except OSError as exc:
            self.log(f"❌ {exc}")
            self._set_status("保存目录不可用", COLOR_BAD)
            return False
        # 已经在跑：配置没变就复用，变了（目录/端口/账号/本机 IP）就重启，
        # 否则会出现"界面上改了目录、打印机却还在往老目录写"的诡异现象。
        if self.ftp is not None and self.ftp.running:
            desired_user = "anonymous" if self.anon_var.get() else (self.ftp_user_var.get().strip() or "")
            desired_pwd = "" if self.anon_var.get() else self.ftp_pwd_var.get()
            same = (
                save_dir
                and os.path.abspath(self.ftp.root_dir) == os.path.abspath(save_dir)
                and str(self.ftp.port) == (self.port_var.get().strip() or "21")
                and self.ftp.username == (desired_user or "anonymous")
                and self.ftp.password == desired_pwd
                and (self.ftp.masquerade_ip or "") == (self._current_ip() or "")
            )
            if same:
                self._refresh_ftp_state()
                return True
            self.log("FTP 配置有变化，重启接收服务…")
            self._stop_ftp()
        if not save_dir:
            messagebox.showwarning(APP_TITLE, "请先选择保存目录")
            return False
        try:
            port = int(self.port_var.get().strip() or 21)
        except ValueError:
            messagebox.showwarning(APP_TITLE, "监听端口必须是数字")
            return False

        anonymous = self.anon_var.get()
        # 端口被占用（10048）很常见：上一份实例还收在托盘里、或旧的 Slyar 服务没关。
        # 自动换一个端口继续（打印机条目里写的是端口号，注册时会一起更新）。
        candidates = [port] + [p for p in (2121, 2122, 2123, 2124) if p != port]
        server = None
        for index, candidate in enumerate(candidates):
            attempt = FtpScanServer(
                root_dir=save_dir,
                port=candidate,
                username=self.ftp_user_var.get().strip() or "anonymous",
                password=self.ftp_pwd_var.get(),
                log=self.log,
                on_file=self._on_file_received,
            )
            attempt.masquerade_ip = self._current_ip() or netinfo.choose_local_ip(
                self.printer_var.get().strip()
            )
            if attempt.start():
                server = attempt
                if candidate != port:
                    self.log(f"⚠ 端口 {port} 被占用，已自动改用 {candidate}（打印机条目会写这个端口）")
                    self.port_var.set(str(candidate))
                    self.cfg.port = candidate
                break
            occupied = "10048" in (attempt.last_error or "") or "被占用" in (attempt.last_error or "")
            if index == 0 and occupied:
                self.log(f"⚠ {attempt.last_error}")
                continue
            if index == 0:
                self._set_status(f"FTP 启动失败：{attempt.last_error}", COLOR_BAD)
                return False
            self.log(f"⚠ 端口 {candidate} 也不可用：{attempt.last_error}")
        if server is None:
            self._set_status("FTP 启动失败：没有可用端口", COLOR_BAD)
            return False
        self.ftp = server
        self._set_status(server.status_line(), COLOR_OK)

        ok, messages = firewall.ensure_rules(server.port, server.passive_ports)
        for line in messages:
            self.log(line)
        if not ok:
            self.log("提示：防火墙未确认放通，若打印机提示连不上文件夹，请以管理员身份重启本程序。")

        self._update_tray_tooltip()
        self.ftp_hint.configure(
            text=(
                f"打印机里要填的地址：{server.masquerade_ip}:{server.port}"
                f"　路径 {self.cfg.ftp_path}　账号 {'anonymous（空密码）' if server.is_anonymous else server.username}"
            )
        )
        if not silent:
            self.log(f"FTP 服务已启动，扫描件会保存到：{save_dir}")
        return True

    def _stop_ftp(self) -> None:
        if self.ftp is not None:
            self.ftp.stop()
            self.ftp = None
        self._set_status("未启动", COLOR_IDLE)
        self._update_tray_tooltip()

    def _self_check(self) -> None:
        if self.ftp is None or not self.ftp.running:
            messagebox.showinfo(APP_TITLE, "请先启动 FTP 服务再自检")
            return
        ok, detail = self.ftp.self_check()
        self.log(("✅ " if ok else "❌ ") + detail)
        messagebox.showinfo(APP_TITLE, detail)

    def _on_file_received(self, item: ReceivedFile) -> None:
        size_kb = max(1, round(item.size / 1024))
        self.log(f"📄 收到扫描文件：{item.path}（{size_kb} KB）")

    # ================================================================ 打印机
    def _test_printer(self) -> None:
        host = self.printer_var.get().strip()
        if not host:
            messagebox.showwarning(APP_TITLE, "请填写打印机地址")
            return

        def worker() -> None:
            ok, detail = probe_printer(host)
            self.log(("✅ 打印机连通：" if ok else "❌ ") + detail)
            self.root.after(0, lambda: messagebox.showinfo(APP_TITLE, detail))

        threading.Thread(target=worker, daemon=True).start()

    def _open_wim(self) -> None:
        host = self.printer_var.get().strip()
        if not host:
            messagebox.showwarning(APP_TITLE, "请填写打印机地址")
            return
        webbrowser.open(f"http://{host}{WIM_PATH}")

    def _relaunch_admin(self) -> None:
        script = os.path.join(self.base_dir, "main.py")
        if not os.path.isfile(script):
            messagebox.showerror(APP_TITLE, f"找不到入口文件：{script}")
            return
        try:
            params = f'"{script}"'
            ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, self.base_dir, 1)
            self.root.after(300, self.root.destroy)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"提权失败：{exc}")

    # ================================================================ 一键注册
    def _on_go(self) -> None:
        if self.busy:
            return
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "请填写目的地名字")
            self.name_entry.focus_set()
            return
        printer_host = self.printer_var.get().strip()
        if not printer_host:
            messagebox.showwarning(APP_TITLE, "请填写打印机地址")
            return
        save_dir = self.dir_var.get().strip()
        if not save_dir:
            messagebox.showwarning(APP_TITLE, "请选择保存目录")
            return
        try:
            save_dir = self._pick_writable_dir(save_dir)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self._collect_config()
        save_config(self.base_dir, self.cfg)

        if not self._start_ftp(silent=True):
            return

        self.busy = True
        self.go_btn.configure(state="disabled")
        self._set_status("正在注册…", COLOR_BUSY)
        self.log(f"===== 开始注册「{name}」 =====")
        # Tk 变量只能在主线程读，先取出来再交给工作线程
        local_ip = self._current_ip()
        threading.Thread(target=self._register_worker, args=(name, local_ip), daemon=True).start()

    def _ensure_browser(self, headless: bool) -> CdpBrowser:
        """拿到本会话唯一的浏览器实例，死了才重建。"""
        if self._browser is not None and self._browser.is_alive():
            return self._browser
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        browser = CdpBrowser(headless=headless, log=self.log)
        browser.start()
        self._browser = browser
        self.log("[浏览器] 已启动一次，本次会话内复用（不再反复新开）")
        return browser

    def _register_worker(self, name: str, local_ip: str) -> None:
        cfg = self.cfg
        try:
            local_ip = local_ip or netinfo.choose_local_ip(cfg.printer_host)
            if self.ftp is not None:
                self.ftp.masquerade_ip = local_ip

            try:
                browser = self._ensure_browser(headless=not cfg.show_browser)
            except Exception as exc:
                self.log(f"❌ 浏览器启动失败：{exc}")
                self.root.after(0, lambda: self._on_register_done(
                    RegistrationResult(ok=False, message=f"浏览器启动失败：{exc}"), name))
                return

            registrar = RicohAddressBookRegistrar(
                host=cfg.printer_host,
                admin_user=cfg.admin_user,
                admin_password=cfg.admin_password,
                ftp_host=local_ip or "",
                ftp_port=cfg.port,
                ftp_user="anonymous" if cfg.anonymous else cfg.ftp_user,
                ftp_password="" if cfg.anonymous else cfg.ftp_password,
                ftp_path=cfg.ftp_path,
                headless=not cfg.show_browser,
                step_wait_ms=cfg.step_wait_ms,
                log=self.log,
                debug_dir=os.path.join(self.log_dir, "debug"),
                add_to_favorites=cfg.add_to_favorites,
                browser=browser,
                merge_by_ip=cfg.merge_by_ip,
            )
            result = registrar.register(name, self.entry_no_var.get().strip())
        except Exception as exc:  # 兜底：绝不让后台线程静默死掉
            result = RegistrationResult(ok=False, message=f"未预期错误：{exc}")
        self.last_result = result
        self.root.after(0, lambda: self._on_register_done(result, name))

    def _on_register_done(self, result: RegistrationResult, name: str) -> None:
        self.busy = False
        self.go_btn.configure(state="normal")
        self.log("----- 结果 -----")
        for step in result.steps:
            self.log(("✅ " if step.ok else "❌ ") + step.name + (f"：{step.detail}" if step.detail else ""))

        if result.ok:
            self._set_status(f"注册成功：{name}", COLOR_OK)
            self.log(f"✅ 注册成功，注册编号 {result.entry_no}")
            open_dir = messagebox.askyesno(
                APP_TITLE,
                f"「{name}」已注册到打印机。\n\n"
                f"现在到打印机面板：按【扫描】→ 选择【{name}】→ 开始扫描，\n"
                f"文件会直接进：{self.dir_var.get()}\n\n是否现在打开这个文件夹？",
            )
            if open_dir:
                self._open_dir()
        else:
            self._set_status("注册失败", COLOR_BAD)
            self.log("❌ " + result.message)
            tip = ""
            if result.debug_dir:
                tip = f"\n\n排障快照：{result.debug_dir}\n（里面是每一步的截图和 HTML，发我可直接定位）"
            messagebox.showerror(APP_TITLE, result.message + tip)

    # ================================================================ 收尾
    def _on_close(self) -> None:
        # 有托盘时，点窗口 X 只是收起来，FTP 继续跑；退出请用托盘右键菜单
        if self.tray is not None and not self._exiting:
            self.root.withdraw()
            self.log("窗口已收进托盘，FTP 继续运行；右键托盘图标可停止服务或退出")
            return
        self._closing = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except Exception:
                pass
        try:
            self._collect_config()
            save_config(self.base_dir, self.cfg)
        except Exception:
            pass
        if self.ftp is not None:
            self.ftp.stop()
        if self._browser is not None:
            self._logout_printer_session()
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        self.root.destroy()


def run_cli(base_dir: str, argv: list) -> int:
    """无界面跑一次注册，用于现场/自动化验证打包版：

        扫描直连.exe --cli-register 张三 [--printer 192.168.1.100] [--password xxx]

    结果写到 logs\\cli.log（窗口版 exe 没有控制台，只能落文件），退出码 0/1。
    """
    args = list(argv)
    name = ""
    printer = ""
    password = None
    if "--cli-register" in args:
        index = args.index("--cli-register")
        if index + 1 < len(args):
            name = args[index + 1]
    for flag, target in (("--printer", "printer"), ("--password", "password")):
        if flag in args:
            index = args.index(flag)
            if index + 1 < len(args):
                if target == "printer":
                    printer = args[index + 1]
                else:
                    password = args[index + 1]

    os.makedirs(os.path.join(base_dir, "logs"), exist_ok=True)
    log_path = os.path.join(base_dir, "logs", "cli.log")
    cfg = load_config(base_dir)
    host = printer or cfg.printer_host
    pwd = cfg.admin_password if password is None else password
    lines: list = []

    def log(message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        text = f"[{stamp}] {message}"
        lines.append(text)
        # 控制台可能是 GBK 代码页，✓/⚠ 这类字符会抛 UnicodeEncodeError，不能让日志把流程搞崩
        try:
            print(text, flush=True)
        except (UnicodeEncodeError, OSError):
            pass
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(text + "\n")
        except OSError:
            pass

    if not name:
        log("缺少目的地名字：--cli-register 名字")
        return 2
    log(f"CLI 注册开始：名字={name} 打印机={host}")
    local_ip = netinfo.choose_local_ip(host)
    log(f"本机给打印机用的地址：{local_ip}")
    ok, detail = probe_printer(host)
    log(f"探测打印机：{detail}")
    if not ok:
        return 1

    browser = None
    registrar = None
    try:
        browser = CdpBrowser(headless=not cfg.show_browser, log=log, verbose=True)
        browser.start()
        registrar = RicohAddressBookRegistrar(
            host=host,
            admin_user=cfg.admin_user,
            admin_password=pwd,
            ftp_host=local_ip or "",
            ftp_port=cfg.port,
            ftp_user="anonymous" if cfg.anonymous else cfg.ftp_user,
            ftp_password="" if cfg.anonymous else cfg.ftp_password,
            ftp_path=cfg.ftp_path,
            headless=not cfg.show_browser,
            step_wait_ms=cfg.step_wait_ms,
            log=log,
            debug_dir=os.path.join(base_dir, "logs", "debug"),
            browser=browser,
            merge_by_ip=cfg.merge_by_ip,
        )
        result = registrar.register(name)
        for line in result.summary().splitlines():
            log(line)
        log(f"结果：{'成功' if result.ok else '失败'} | 编号={result.entry_no} | {result.message}")
        try:
            registrar.logout()
        except Exception:
            pass
        return 0 if result.ok else 1
    except Exception as exc:
        log(f"异常：{exc!r}")
        return 1
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def main(base_dir: Optional[str] = None) -> None:
    enable_hi_dpi()
    base = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = tk.Tk()
    App(root, base)
    root.mainloop()
