"""配置持久化：把上次填的东西记住，现场第二次用就不用再输。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from typing import Any


def desktop_dir() -> str:
    """取当前用户的桌面目录。

    不能直接拼 `~/Desktop`：中文系统的桌面叫「桌面」，而且很多人开着 OneDrive
    备份，桌面会被重定向到 `...\\OneDrive\\桌面`。所以读注册表里的实际路径。
    """
    if sys.platform.startswith("win"):
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
                expanded = os.path.expandvars(value)
                if expanded and os.path.isabs(expanded):
                    return expanded
        except (OSError, ImportError, ValueError):
            pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def default_scan_dir() -> str:
    """默认保存目录：桌面\扫描。"""
    return os.path.join(desktop_dir(), "扫描")


@dataclass
class AppConfig:
    save_dir: str = ""
    port: int = 21
    ftp_user: str = "anonymous"
    ftp_password: str = ""
    anonymous: bool = True
    printer_host: str = ""
    admin_user: str = "admin"
    admin_password: str = ""
    last_name: str = ""
    show_browser: bool = False
    ftp_path: str = "/"
    add_to_favorites: bool = True
    step_wait_ms: int = 1200
    # 同名或同一个 FTP 服务器 IP 视为同一条记录，注册时更新而不是新增
    merge_by_ip: bool = True
    # 仅作记录；真实开关以注册表（HKCU Run）为准，见 app/autostart.py
    auto_start: bool = False
    # IP 校验：本机 IP 变动后自动更新打印机里的注册信息
    watch_ip: bool = True


def config_path(base_dir: str) -> str:
    return os.path.join(base_dir, "config.json")


def load_config(base_dir: str) -> AppConfig:
    path = config_path(base_dir)
    if not os.path.isfile(path):
        return AppConfig()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return AppConfig()
    known = {f.name for f in fields(AppConfig)}
    clean = {k: v for k, v in (data or {}).items() if k in known}
    try:
        return AppConfig(**clean)
    except TypeError:
        return AppConfig()


def save_config(base_dir: str, cfg: AppConfig) -> None:
    path = config_path(base_dir)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(cfg), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass
