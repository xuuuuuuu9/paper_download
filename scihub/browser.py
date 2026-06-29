"""Manual-verification fallback: open a real browser to clear a challenge.

Used only when the headless TLS-impersonation path hits a captcha/challenge.
The user solves it once; we harvest the resulting cookies so subsequent
requests (this run and future runs) reuse the cleared session.
"""
from __future__ import annotations


def solve_with_browser(url: str, mirror: str, existing_cookies: dict[str, str] | None = None) -> dict[str, str]:
    """Launch Chromium, let the user pass the challenge, return fresh cookies.

    Returns an empty dict if Playwright is unavailable or the user aborts, so
    the caller can degrade gracefully instead of crashing the whole batch.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(
            "    [手动验证] 未安装 playwright，无法打开浏览器。\n"
            "    请运行：pip install playwright && playwright install chromium"
        )
        return {}

    domain = f".{mirror}"
    print(f"    [手动验证] 正在为 {mirror} 打开浏览器，请在窗口中完成人机验证…")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context()

            if existing_cookies:
                context.add_cookies([
                    {"name": name, "value": value, "domain": domain, "path": "/"}
                    for name, value in existing_cookies.items()
                ])

            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)

            input(
                "    >>> 验证完成、页面显示论文后，回到终端按【回车】继续… "
            )

            cookies = {c["name"]: c["value"] for c in context.cookies()}
            browser.close()
            return cookies
    except Exception as exc:
        print(f"    [手动验证] 浏览器流程出错: {exc}")
        return {}
