"""只读探查真实打印机后台：结构、frame、页面文字、截图。

不做任何点击，只打开首页 + 登录页，把现场情况抓下来看。
运行： python tests\\inspect_printer.py 192.168.1.100
"""

from __future__ import annotations

import os
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (BASE_DIR, os.path.join(BASE_DIR, "vendor")):
    if os.path.isdir(path) and path not in sys.path:
        sys.path.insert(0, path)

import requests  # noqa: E402

from app.cdp import CdpBrowser  # noqa: E402
from app.printer_ricoh import WIM_PATH  # noqa: E402

OUT_DIR = os.path.join(BASE_DIR, "logs", "real")


def dump_http(host: str) -> None:
    session = requests.Session()
    session.trust_env = False
    for path in (WIM_PATH, "/web/guest/cn/websys/webArch/mainFrame.cgi",
                 "/web/guest/en/websys/webArch/mainFrame.cgi", "/"):
        url = f"http://{host}{path}"
        try:
            resp = session.get(url, timeout=8, allow_redirects=True, proxies={"http": None, "https": None})
        except Exception as exc:
            print(f"[HTTP] {path} -> 异常 {exc}")
            continue
        text = resp.text or ""
        print(f"\n[HTTP] {path} -> {resp.status_code}, {len(text)} bytes, final={resp.url}")
        lowered = text.lower()
        for key in ("web image monitor", "ricoh", "frameset", "login", "form"):
            print(f"       含 {key!r}: {key in lowered}")
        title = ""
        start = lowered.find("<title>")
        if start >= 0:
            title = text[start:lowered.find("</title>", start) + 8]
        print("       title:", title[:120].replace("\n", " "))
        if path == WIM_PATH:
            with open(os.path.join(OUT_DIR, "mainFrame.html"), "w", encoding="utf-8", errors="ignore") as fh:
                fh.write(text)
    session.close()


def dump_browser(host: str) -> None:
    browser = CdpBrowser(headless=True, log=lambda m: None)
    browser.start()
    try:
        browser.navigate(f"http://{host}{WIM_PATH}", wait_ready=True, ready_timeout=30)
        print("\n[BROWSER] 打开完成")
        tree = browser.send("Page.getFrameTree")
        print("  frameTree:", str(tree)[:400])
        print("  frame_contexts:", {fid[:8]: cid for fid, cid in browser.frame_contexts.items()})
        text = browser.page_text()
        print("  page_text:", text[:600].replace("\n", " | "))
        for cid in list(browser.frame_contexts.values()):
            try:
                url = browser._eval_in(cid, "location.href")
                title = browser._eval_in(cid, "document.title")
                print(f"  ctx {cid}: title={title!r} url={url!r}")
            except Exception as exc:
                print(f"  ctx {cid}: <err {exc}>")

        shot = os.path.join(OUT_DIR, "01-home.png")
        browser.screenshot(shot)
        print("  截图:", shot)

        # 找登录入口：只识别，不点击
        found = browser.evaluate_all(
            """() => {
                const norm = s => (s || '').replace(/[\\s\\u00a0]+/g, '');
                const hits = [];
                for (const el of document.querySelectorAll('a,button,input,area,img,span,div,td')) {
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) continue;
                    const label = norm(el.value) || norm(el.getAttribute('alt'))
                        || norm(el.getAttribute('title')) || norm(el.innerText);
                    if (label && (label.includes('登录') || label.includes('Login') ||
                                  label.includes('ログイン'))) {
                        hits.push(el.tagName + ':' + label.slice(0, 20));
                    }
                }
                return hits.length ? hits : null;
            }"""
        )
        print("  疑似登录入口:", found)
        print("  html 长度:", len(browser.html_dump()))
    finally:
        browser.close()


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
    os.makedirs(OUT_DIR, exist_ok=True)
    print(f"=== 探查 {host} ===")
    dump_http(host)
    dump_browser(host)
    print("\n完成。截图与 HTML 在:", OUT_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
