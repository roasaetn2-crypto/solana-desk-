"""Screenshot the tabbed dashboard at desktop and mobile widths, and prove
that clicking a tab actually swaps the visible column on narrow screens."""
import asyncio
from playwright.async_api import async_playwright

URL = "http://localhost:8000/"
OUT = "/workspace/.screenshots"


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()

        # ---- desktop ----
        page = await browser.new_page(viewport={"width": 1440, "height": 900})
        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=f"{OUT}/tabs_desktop.png", full_page=False)

        # ---- mobile ----
        m = await browser.new_page(viewport={"width": 390, "height": 844})
        await m.goto(URL, wait_until="networkidle")
        await m.wait_for_timeout(1500)
        await m.screenshot(path=f"{OUT}/tabs_mobile_live.png", full_page=False)

        # visible column on mobile should be the live one
        vis = await m.evaluate("""() => {
          const cols = ['.col-left','.col-center','.col-right'];
          return cols.filter(s => {
            const el = document.querySelector(s);
            return el && getComputedStyle(el).display !== 'none';
          });
        }""")
        print("mobile visible cols (live tab):", vis)

        # click Control
        await m.click('.desk-tab[data-tab="control"]')
        await m.wait_for_timeout(700)
        await m.screenshot(path=f"{OUT}/tabs_mobile_control.png", full_page=False)
        vis2 = await m.evaluate("""() => {
          const cols = ['.col-left','.col-center','.col-right'];
          return cols.filter(s => {
            const el = document.querySelector(s);
            return el && getComputedStyle(el).display !== 'none';
          });
        }""")
        print("mobile visible cols (control tab):", vis2)

        # click Research
        await m.click('.desk-tab[data-tab="research"]')
        await m.wait_for_timeout(700)
        await m.screenshot(path=f"{OUT}/tabs_mobile_research.png", full_page=False)
        vis3 = await m.evaluate("""() => {
          const cols = ['.col-left','.col-center','.col-right'];
          return cols.filter(s => {
            const el = document.querySelector(s);
            return el && getComputedStyle(el).display !== 'none';
          });
        }""")
        print("mobile visible cols (research tab):", vis3)

        # persistence check: reload should keep research
        await m.reload(wait_until="networkidle")
        await m.wait_for_timeout(800)
        tab = await m.evaluate("document.getElementById('deskLayout').dataset.tab")
        print("tab after reload:", tab)

        await browser.close()


asyncio.run(main())
