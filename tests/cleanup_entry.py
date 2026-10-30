"""按名字删除打印机通讯簿里的条目（清理测试残留用）。

运行： python tests\\cleanup_entry.py 192.168.1.100 扫描直连测试 [密码]
"""

from __future__ import annotations

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BASE_DIR, os.path.join(BASE_DIR, "vendor")):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

from app.cdp import CdpBrowser  # noqa: E402
from app.printer_ricoh import RicohAddressBookRegistrar  # noqa: E402


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
    name = sys.argv[2] if len(sys.argv) > 2 else "扫描直连测试"
    password = sys.argv[3] if len(sys.argv) > 3 else ""

    browser = CdpBrowser(headless=True)
    browser.start()
    reg = RicohAddressBookRegistrar(
        host=host, admin_password=password, ftp_host="192.168.1.10",
        step_wait_ms=1200, log=lambda m: print("   ", m), browser=browser,
    )
    try:
        reg._goto_main()
        reg._login()
        reg._open_address_book()
        entries = reg._read_address_list()
        targets = [(no, nm) for no, nm in entries if nm == name]
        print(f"通讯簿共 {len(entries)} 条，匹配「{name}」: {targets}")
        if not targets:
            print("没有需要清理的条目")
            return 0
        for no, _nm in targets:
            deleted = reg._delete_entry(no)
            print(f"删除 {no}: {'成功' if deleted else '失败'}")
        left = reg._read_address_list()
        remain = [(no, nm) for no, nm in left if nm == name]
        print(f"清理后共 {len(left)} 条，残留: {remain}")
        return 0 if not remain else 1
    finally:
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
