"""临时开一个真实 FTP 接收服务，用来验证打印机能不能把扫描件传过来。

用途：真机上确认「扫描 → 选目的地 → 文件落到本机」这条链路。
运行： python tests\\serve_for_scan.py [分钟数]
"""

from __future__ import annotations

import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BASE_DIR, os.path.join(BASE_DIR, "vendor")):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

from app import firewall, netinfo  # noqa: E402
from app.ftp_server import FtpScanServer  # noqa: E402

PRINTER = "192.168.1.100"


def main() -> int:
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0
    save_dir = os.path.join(BASE_DIR, "logs", "real", "扫描件")
    os.makedirs(save_dir, exist_ok=True)

    local_ip = netinfo.choose_local_ip(PRINTER)
    print(f"[准备] 本机给打印机用的地址: {local_ip}", flush=True)
    print(f"[准备] 保存目录: {save_dir}", flush=True)

    ok, messages = firewall.ensure_rules(21, range(30000, 30100))
    for line in messages:
        print("[防火墙]", line, flush=True)
    print("[防火墙] 全部放通:", ok, flush=True)

    server = FtpScanServer(
        root_dir=save_dir,
        port=21,
        log=lambda m: print("[FTP]", m, flush=True),
        on_file=lambda item: print(
            f"[收到] {item.path} ({max(1, round(item.size / 1024))} KB)", flush=True
        ),
    )
    server.masquerade_ip = local_ip
    if not server.start():
        print("[失败]", server.last_error, flush=True)
        return 1
    checked, detail = server.self_check()
    print("[自检]", detail, flush=True)

    print("=" * 60, flush=True)
    print(f"服务已就绪：ftp://{local_ip}:21/  用户 anonymous 空密码", flush=True)
    print("现在到打印机面板操作：扫描 → 选择「扫描直连测试」→ 开始扫描", flush=True)
    print(f"等待 {minutes:g} 分钟，收到文件会立刻打印在这里。按 Ctrl+C 提前结束。", flush=True)
    print("=" * 60, flush=True)

    deadline = time.time() + minutes * 60
    try:
        while time.time() < deadline:
            time.sleep(2)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        print("[结束] FTP 已停止；共收到", len(server.received), "个文件", flush=True)
        for item in server.received:
            print("  -", item.path, item.size, "bytes", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
