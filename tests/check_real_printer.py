"""对真实打印机做只读检查：登录 + 进通讯簿 + 读地址列表，不写入任何东西。

用于在真机上验证自动化选择器（比猜可靠）。每一步都截图到 logs/real/。
运行： python tests\\check_real_printer.py 192.168.1.100 [管理员密码]
"""

from __future__ import annotations

import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BASE_DIR, os.path.join(BASE_DIR, "vendor")):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

from app.cdp import CdpBrowser  # noqa: E402
from app.printer_ricoh import RicohAddressBookRegistrar  # noqa: E402

OUT_DIR = os.path.join(BASE_DIR, "logs", "real")


def show(browser: CdpBrowser, tag: str) -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    shot = os.path.join(OUT_DIR, f"{tag}.png")
    browser.screenshot(shot)
    print(f"\n--- {tag} ---")
    print("  frames:", {fid[:8]: cid for fid, cid in browser.frame_contexts.items()})
    print("  text  :", browser.page_text()[:500].replace("\n", " | "))
    print("  shot  :", shot)


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
    password = sys.argv[2] if len(sys.argv) > 2 else ""

    browser = CdpBrowser(headless=True, log=lambda m: None)
    browser.start()
    registrar = RicohAddressBookRegistrar(
        host=host,
        admin_password=password,
        ftp_host="192.168.1.10",
        step_wait_ms=1200,
        log=lambda m: print("  log:", m),
    )
    registrar.browser = browser
    try:
        registrar._goto_main()
        show(browser, "10-main")

        try:
            registrar._login()
            print("\n✅ 管理员登录成功")
        except Exception as exc:
            print(f"\n❌ 登录失败：{exc}")
            show(browser, "11-login-failed")
            return 1
        show(browser, "12-after-login")

        try:
            registrar._open_address_book()
            print("\n✅ 进入通讯簿")
        except Exception as exc:
            print(f"\n❌ 进通讯簿失败：{exc}")
            show(browser, "13-addressbook-failed")
            return 1
        show(browser, "14-address-list")

        entries = registrar._read_address_list()
        print(f"\n📇 读到地址条目 {len(entries)} 条，前 10 条：")
        for no, name in entries[:10]:
            print(f"   {no}  {name}")
        if entries:
            nums = [int(no) for no, _ in entries if no.isdigit()]
            print("   最大注册编号:", max(nums), "→ 下一个自动分配:", f"{max(nums)+1:05d}")
        print("\n（只读检查结束，没有写入任何东西）")
        return 0
    finally:
        browser.close()


if __name__ == "__main__":
    sys.exit(main())
