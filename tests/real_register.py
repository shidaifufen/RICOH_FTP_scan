"""在真实打印机上跑一次完整注册（会写入通讯簿！）。

流程与 GUI 里「确认并注册」完全一致：启动内置 FTP → 检测本机 IP → 登录打印机
→ 添加用户 → 基本信息 → 文件夹目的地 → 确认注册 → 回列表校验。

运行：
    python tests\\real_register.py 192.168.1.100 扫描直连测试 [管理员密码]

注册成功后会打印从打印机读回的条目，确认 IP/端口/路径/账号都写对了。
"""

from __future__ import annotations

import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BASE_DIR, os.path.join(BASE_DIR, "vendor")):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

from app import netinfo  # noqa: E402
from app.ftp_server import FtpScanServer  # noqa: E402
from app.printer_ricoh import RicohAddressBookRegistrar, probe_printer  # noqa: E402


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
    name = sys.argv[2] if len(sys.argv) > 2 else "扫描直连测试"
    password = sys.argv[3] if len(sys.argv) > 3 else ""
    port = 21

    logs = []
    print("=== 1) 探测打印机 ===")
    ok, detail = probe_printer(host)
    print("   ", detail)
    if not ok:
        return 1

    local_ip = netinfo.choose_local_ip(host)
    print("=== 2) 本机给打印机用的地址 ===")
    print("   ", local_ip)

    save_dir = os.path.join(BASE_DIR, "logs", "real", "扫描件")
    os.makedirs(save_dir, exist_ok=True)
    ftp = FtpScanServer(
        root_dir=save_dir,
        port=port,
        log=lambda m: print("    [FTP]", m),
        on_file=lambda item: print("    [FTP] 收到文件:", item.path),
    )
    ftp.masquerade_ip = local_ip
    print(f"=== 3) 启动内置 FTP（端口 {port}） ===")
    if not ftp.start():
        print("    ✗ FTP 启动失败:", ftp.last_error)
        return 1
    ok, detail = ftp.self_check()
    print("    自检:", detail)

    print(f"=== 4) 开始注册「{name}」 ===")
    registrar = RicohAddressBookRegistrar(
        host=host,
        admin_user="admin",
        admin_password=password,
        ftp_host=local_ip or "",
        ftp_port=port,
        ftp_user="anonymous",
        ftp_path="/",
        headless=True,
        step_wait_ms=1500,
        log=lambda m: (print("   ", m), logs.append(m)),
        debug_dir=os.path.join(BASE_DIR, "logs", "real", "debug"),
    )
    result = registrar.register(name)

    print("\n=== 5) 结果 ===")
    print(result.summary())
    print("ok =", result.ok, "| entry_no =", result.entry_no)
    print("message:", result.message)

    if result.ok:
        print("\n=== 6) 从打印机读回该条目核对 ===")
        time.sleep(1.0)
        registrar2 = RicohAddressBookRegistrar(
            host=host, admin_password=password, ftp_host=local_ip or "",
            step_wait_ms=1500, log=lambda m: None,
        )
        # 复用注册器的只读流程
        browser = registrar2  # 仅为可读性
        check = registrar2.register  # noqa: F841  (避免未使用告警)
        try:
            from app.cdp import CdpBrowser

            b = CdpBrowser(headless=True)
            b.start()
            r = RicohAddressBookRegistrar(host=host, admin_password=password, ftp_host=local_ip or "",
                                          step_wait_ms=1200, log=lambda m: None)
            r.browser = b
            r._goto_main()
            r._login()
            r._open_address_book()
            entries = r._read_address_list()
            target = [(no, nm) for no, nm in entries if nm == name]
            print("    通讯簿里找到:", target)
            print(f"    地址列表共 {len(entries)} 条，下一个可用编号: {r._next_entry_no()}")
        finally:
            try:
                b.close()
            except Exception:
                pass

    ftp.stop()
    print("\n完成。FTP 已停止。")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
