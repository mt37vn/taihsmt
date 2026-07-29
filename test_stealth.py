"""Compare default vs stealth Playwright headless detection"""
from playwright.sync_api import sync_playwright

TEST_URL = "https://www.google.com"  # quick test page

print("=== DEFAULT HEADLESS ===")
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(TEST_URL, wait_until="domcontentloaded", timeout=10000)
    wd = page.evaluate("navigator.webdriver")
    print(f"  navigator.webdriver: {wd}")
    browser.close()

print()
print("=== STEALTH HEADLESS ===")
with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-web-security",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
    )
    ctx = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        locale="vi-VN",
        timezone_id="Asia/Ho_Chi_Minh",
    )
    ctx.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        """
    )
    page = ctx.new_page()
    page.goto(TEST_URL, wait_until="domcontentloaded", timeout=10000)
    wd = page.evaluate("navigator.webdriver")
    print(f"  navigator.webdriver: {wd}")
    ctx.close()
    browser.close()
