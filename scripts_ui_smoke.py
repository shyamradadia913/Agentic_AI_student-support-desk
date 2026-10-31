"""Optional browser smoke test (needs: pip install playwright && playwright install chromium).
Start the app first (python -m uvicorn app.main:app --port 8765), then: python scripts_ui_smoke.py"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    b = p.chromium.launch(); errs = []
    pg = b.new_page(); pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:8765/"); pg.fill("#u", "s1004"); pg.fill("#p", "Demo#1004!"); pg.click("#loginform button")
    pg.wait_for_selector(".starter"); pg.click("text=What is my current fee status?"); pg.wait_for_selector(".fu")
    assert pg.locator(".fu").count() >= 1, "no follow-up chips"
    pg.locator(".fu").first.click(); pg.wait_for_function("document.querySelectorAll('.row.bot').length>=2 && !document.querySelector('.dots')")
    pg.fill("#msg", "Show me the fee status of S1003"); pg.keyboard.press("Enter"); pg.wait_for_selector(".card.deny")
    assert not errs, errs
    print("UI smoke test passed"); b.close()
