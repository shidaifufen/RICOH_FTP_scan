"""真机验证「同名/同 IP 只保留一条 + 一次会话只开一个浏览器」。

流程：
  1. 启动**一个**浏览器，注册「扫描直连测试」→ 新增一条
  2. 复用同一个浏览器再注册同名 → 应该更新（先建后删），净剩一条
  3. 最后把测试条目删掉，保持打印机干净
运行： python tests\\real_upsert.py 192.168.1.100 [密码]
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

NAME = "扫描直连测试"


def make_registrar(host: str, password: str, browser: CdpBrowser, logs: list) -> RicohAddressBookRegistrar:
    return RicohAddressBookRegistrar(
        host=host,
        admin_user="admin",
        admin_password=password,
        ftp_host="192.168.1.10",
        ftp_port=21,
        ftp_user="anonymous",
        ftp_path="/",
        headless=True,
        step_wait_ms=1500,
        log=lambda m: (print("   ", m), logs.append(m)),
        debug_dir=os.path.join(BASE_DIR, "logs", "real", "debug"),
        browser=browser,
        merge_by_ip=True,
    )


def count_entries(host: str, password: str, browser: CdpBrowser) -> tuple[int, list]:
    reg = RicohAddressBookRegistrar(host=host, admin_password=password, ftp_host="192.168.1.10",
                                   step_wait_ms=1200, log=lambda m: None, browser=browser)
    reg._goto_main()
    reg._login()
    reg._open_address_book()
    entries = reg._read_address_list()
    return len(entries), entries


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
    password = sys.argv[2] if len(sys.argv) > 2 else ""
    logs: list = []

    browser = CdpBrowser(headless=True, log=lambda m: None)
    browser.start()
    print("浏览器已启动（本进程只启动这一次）")
    try:
        # ---- 第 1 次：新增 ----
        print(f"\n=== 第 1 次注册「{NAME}」 ===")
        r1 = make_registrar(host, password, browser, logs).register(NAME)
        print("   ok =", r1.ok, "| entry_no =", r1.entry_no)
        print("   message:", r1.message)
        if not r1.ok:
            print(r1.summary())
            return 1
        count1, entries1 = count_entries(host, password, browser)
        matched1 = [e for e in entries1 if e[1] == NAME]
        print(f"   通讯簿共 {count1} 条，其中「{NAME}」: {matched1}")

        # ---- 第 2 次：同名，应更新 ----
        print(f"\n=== 第 2 次注册同名「{NAME}」（应更新而非新增） ===")
        r2 = make_registrar(host, password, browser, logs).register(NAME)
        print("   ok =", r2.ok, "| entry_no =", r2.entry_no)
        print("   message:", r2.message)
        if not r2.ok:
            print(r2.summary())
            return 1
        count2, entries2 = count_entries(host, password, browser)
        matched2 = [e for e in entries2 if e[1] == NAME]
        print(f"   通讯簿共 {count2} 条，其中「{NAME}」: {matched2}")
        print(f"   条目总数变化: {count1} -> {count2}（应为 0）")
        print(f"   同名条目数: {len(matched1)} -> {len(matched2)}（应为 1）")

        reuse = [m for m in logs if "复用已启动的浏览器" in m]
        print(f"\n浏览器复用日志命中: {len(reuse)} 次（应 >= 1）")

        ok_upsert = (count2 == count1) and (len(matched2) == 1)
        print("\n结论:", "✅ 同名更新行为正确" if ok_upsert else "❌ 同名更新行为不符合预期")

        # ---- 清理：删掉测试条目 ----
        print(f"\n=== 清理：删除测试条目 ===")
        reg = make_registrar(host, password, browser, logs)
        reg._goto_main()
        reg._login()
        reg._open_address_book()
        reg._read_address_list()
        target = reg._find_existing(NAME)
        if target:
            deleted = reg._delete_entry(target[0])
            print(f"   删除 {target[0]}: {deleted}")
        count3, entries3 = count_entries(host, password, browser)
        print(f"   清理后通讯簿共 {count3} 条，残留「{NAME}」: {[e for e in entries3 if e[1] == NAME]}")
        return 0 if ok_upsert else 1
    finally:
        browser.close()
        print("浏览器已关闭")


if __name__ == "__main__":
    sys.exit(main())
