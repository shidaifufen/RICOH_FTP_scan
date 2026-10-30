"""Windows 防火墙放通。

打印机连本机 FTP 走的是入站 TCP：控制端口（默认 21）+ 被动模式数据端口段。
被防火墙拦住时的典型现象是：FTP 能登录（控制连接建立）但一列目录/上传就超时，
或者打印机直接报"无法连接文件夹"。所以这里在服务启动时把两个端口都放通。
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from typing import Tuple

RULE_PREFIX = "扫描直连"


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run(args: list[str]) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="gbk",
            errors="ignore",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        return 1, str(exc)


def _delete_rule(name: str) -> None:
    _run(
        [
            "netsh",
            "advfirewall",
            "firewall",
            "delete",
            "rule",
            f"name={name}",
        ]
    )


def _add_rule(name: str, port: str) -> Tuple[bool, str]:
    code, out = _run(
        [
            "netsh",
            "advfirewall",
            "firewall",
            "add",
            "rule",
            f"name={name}",
            "dir=in",
            "action=allow",
            "protocol=TCP",
            f"localport={port}",
            "profile=any",
        ]
    )
    return code == 0, out.strip()


def ensure_rules(control_port: int, passive_ports: range) -> Tuple[bool, list[str]]:
    """确保控制端口与被动端口段已放通。返回 (是否全部成功, 说明行)。"""
    messages: list[str] = []
    if not sys.platform.startswith("win"):
        return False, ["非 Windows 系统，跳过防火墙配置"]

    passive_spec = f"{passive_ports.start}-{passive_ports.stop - 1}"

    if not is_admin():
        messages.append(
            "当前不是管理员，无法自动添加防火墙规则；如果打印机提示无法连接，"
            "请右键“以管理员身份运行”本程序，或手工放通 TCP "
            f"{control_port} 与 {passive_spec}。"
        )
        return False, messages

    ok_all = True

    ctrl_name = f"{RULE_PREFIX}-FTP-{control_port}"
    _delete_rule(ctrl_name)
    ok, out = _add_rule(ctrl_name, str(control_port))
    ok_all &= ok
    messages.append(f"防火墙：放通 TCP {control_port} {'成功' if ok else '失败 ' + out}")

    if passive_ports.stop > passive_ports.start:
        pasv_name = f"{RULE_PREFIX}-FTP-被动端口"
        _delete_rule(pasv_name)
        ok, out = _add_rule(pasv_name, passive_spec)
        ok_all &= ok
        messages.append(f"防火墙：放通 TCP {passive_spec} {'成功' if ok else '失败 ' + out}")

    return ok_all, messages


def check_rule(port: int) -> bool:
    """粗略判断是否已有放通该端口的入站规则（只用于界面提示）。"""
    code, out = _run(["netsh", "advfirewall", "firewall", "show", "rule", "name=all"])
    if code != 0:
        return False
    block = out.split("\n\n")
    for chunk in block:
        if f"{RULE_PREFIX}-FTP" not in chunk:
            continue
        if f"LocalPort:  {port}" in chunk or f"本地端口:  {port}" in chunk:
            return True
    return False
