"""本机网络信息探测。

核心问题：打印机要通过 IP 连到本机 FTP，所以给打印机的"服务器名称"必须是
**打印机能够路由到的那个网卡地址**。多网卡/虚拟机/蓝牙网卡很容易选错，
这里统一用"向目标 IP 发包，看内核从哪个源地址发出"的办法来判定。
"""

from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class LocalAddress:
    ip: str
    interface: str = ""
    is_primary: bool = False

    def __str__(self) -> str:
        return f"{self.ip} ({self.interface})" if self.interface else self.ip


def _is_usable(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_unspecified:
        return False
    if addr in ipaddress.IPv4Network("169.254.0.0/16"):
        return False
    return True


def local_ip_for(target: str, port: int = 80, timeout: float = 1.5) -> Optional[str]:
    """返回本机到 target 的源地址（UDP connect 不会真的发包）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(timeout)
            s.connect((target, port))
            ip = s.getsockname()[0]
            return ip if _is_usable(ip) else None
    except OSError:
        return None


def _hostname_addresses() -> List[str]:
    ips: List[str] = []
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        ips.extend(addrs)
    except OSError:
        pass
    return ips


def _ipconfig_addresses() -> List[str]:
    """Windows 兜底：解析 ipconfig 拿到所有 IPv4（主机名解析常常漏掉多网卡）。"""
    if not sys.platform.startswith("win"):
        return []
    try:
        out = subprocess.run(
            ["ipconfig"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    import re

    return re.findall(r"IPv4[^:]*:\s*([0-9.]+)", out)


def list_local_addresses() -> List[LocalAddress]:
    """列出本机可用的 IPv4 地址（去重、排序：局域网段优先）。"""
    found: List[str] = []
    for ip in _hostname_addresses() + _ipconfig_addresses():
        if _is_usable(ip) and ip not in found:
            found.append(ip)

    # 用公网/网关目标探测出来的"主地址"排最前
    primary = local_ip_for("8.8.8.8") or local_ip_for("223.5.5.5")
    if primary and primary not in found:
        found.insert(0, primary)

    found.sort(key=lambda ip: (ip != primary, not ip.startswith(("10.", "192.168.", "172.")), ip))
    return [LocalAddress(ip=ip, is_primary=(ip == primary)) for ip in found]


def same_subnet(ip_a: str, ip_b: str, prefix: int = 24) -> bool:
    try:
        a = ipaddress.IPv4Address(ip_a)
        b = ipaddress.IPv4Address(ip_b)
    except ValueError:
        return False
    net = ipaddress.IPv4Network(f"{ip_a}/{prefix}", strict=False)
    return a in net and b in net


def choose_local_ip(printer_ip: str = "", verbose: bool = False) -> Optional[str]:
    """挑一个打印机可达的本机 IP。

    优先级：
      1. 向打印机 IP 发包得到的源地址（最准，跨网段也对）
      2. 与打印机同 /24 的地址
      3. 主地址（默认路由出口）
      4. 任意可用地址
    """
    printer_ip = (printer_ip or "").strip()

    if printer_ip:
        for port in (80, 443, 21, 9100):
            ip = local_ip_for(printer_ip, port)
            if ip:
                if verbose:
                    print(f"[netinfo] 源地址探测 {printer_ip}:{port} -> {ip}")
                return ip

    addresses = [a.ip for a in list_local_addresses()]
    if verbose:
        print(f"[netinfo] 本机地址: {addresses}")

    if printer_ip:
        for ip in addresses:
            if same_subnet(ip, printer_ip):
                return ip

    if addresses:
        return addresses[0]
    return None


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    print("本机地址:")
    for a in list_local_addresses():
        print(f"  - {a}")
    print(f"给打印机用的地址: {choose_local_ip(target, verbose=True)}")
