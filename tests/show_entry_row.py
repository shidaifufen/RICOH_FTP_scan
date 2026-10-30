"""打印真实打印机通讯簿里某个名称的完整行（含"文件夹"列），用于核对写入内容。

运行： python tests\\show_entry_row.py 192.168.1.100 扫描直连测试 [密码]
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
    registrar = RicohAddressBookRegistrar(
        host=host, admin_password=password, ftp_host="192.168.1.10",
        step_wait_ms=1200, log=lambda m: None,
    )
    registrar.browser = browser
    try:
        registrar._goto_main()
        registrar._login()
        registrar._open_address_book()
        registrar._read_address_list()  # 触发加载 + 稳定
        rows = registrar._collect_address_rows()

        print(f"共 {len(rows)} 行（含表头）")
        print("表头:", rows[0] if rows else [])
        hit = [r for r in rows if any(str(cell).strip() == name for cell in r)]
        if not hit:
            print(f"✗ 没找到名称为「{name}」的行")
            return 1
        for row in hit:
            print("匹配行:", row)
            if len(row) >= 7:
                print(f"  → 注册编号={row[1]!r} 名称={row[2]!r} 文件夹={row[6]!r}")

        shot = os.path.join(BASE_DIR, "logs", "real", "15-entry-row.png")
        browser.screenshot(shot)
        print("截图:", shot)
        return 0
    finally:
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
