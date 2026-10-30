"""分阶段计时调试 CDP 启动链路，定位卡在哪一步。

运行： python tests\\debug_cdp.py
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


def step(label: str, start: float) -> float:
    now = time.time()
    print(f"[{now - start:6.2f}s] {label}", flush=True)
    return now


def main() -> int:
    t0 = time.time()
    browser = CdpBrowser(headless=True, log=lambda m: print("   log:", m, flush=True))
    print("启动浏览器…", flush=True)
    try:
        browser.start()
    except Exception as exc:
        step(f"start 失败：{exc}", t0)
        return 1
    t = step("start 完成", t0)

    print(f"   contexts={sorted(browser.contexts.keys())} port={browser._port}", flush=True)
    t = step("读取上下文", t)
    print("   targets:", [x.get("type") for x in browser._targets()], flush=True)
    t = step("列 targets", t)

    import pathlib
    import tempfile
    tmp = tempfile.TemporaryDirectory()
    page = pathlib.Path(tmp.name) / "p.html"
    page.write_text("<html><head><meta charset='utf-8'></head><body><h1>hello 中文</h1><input type=text id=a></body></html>", encoding="utf-8")
    browser.navigate(page.as_uri(), wait_ready=True)
    t = step("navigate", t)
    print("   text:", repr(browser.page_text())[:120], flush=True)
    t = step("page_text", t)

    value = browser.evaluate_all("() => { document.title = 'x'; return document.querySelector('h1').innerText; }")
    print("   heading:", repr(value), flush=True)
    t = step("evaluate_all", t)

    shot = os.path.join(BASE_DIR, "logs", "debug", "cdp-shot.png")
    os.makedirs(os.path.dirname(shot), exist_ok=True)
    print("   screenshot ok:", browser.screenshot(shot), os.path.getsize(shot) if os.path.exists(shot) else 0, flush=True)
    t = step("screenshot", t)

    browser.close()
    step("close 完成", t0)
    print("OK", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
