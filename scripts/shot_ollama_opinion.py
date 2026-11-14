"""Render the second-opinion thread with an Ollama decision and screenshot it.

The live desk has no qualifying candidate right now, so this calls the page's own
brainThreadHtml() with a realistic Ollama opinion and paints the result. It proves
the dashboard labels the local model correctly instead of showing a raw key name.
"""
import asyncio
import json

from playwright.async_api import async_playwright

URL = "http://127.0.0.1:8011/"
OUT = "/workspace/.screenshots/ollama_second_opinion.png"

FAKE = {
    "symbol": "TESTCAT",
    "brain": {
        "symbol": "TESTCAT", "source": "ollama", "agreement": "split", "verdict": "skip",
        "providers": ["ollama", "gemini"],
        "opinions": {
            "ollama": {"verdict": "skip", "confidence": 64,
                       "reasons": ["top-10 holds 71% of supply"], "red_flags": ["concentrated"]},
            "gemini": {"verdict": "watch", "confidence": 58,
                       "reasons": ["volume is real but thin"]},
        },
        "debate": [
            {"bot": "ollama", "say": "71% in ten wallets. I am not buying that.", "verdict": "skip"},
            {"bot": "gemini", "say": "Fair, but the volume is not fake.", "verdict": "watch"},
        ],
    },
}


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        page = await b.new_page(viewport={"width": 900, "height": 720})
        await page.goto(URL, wait_until="networkidle")
        await page.wait_for_timeout(1200)
        html = await page.evaluate("d => brainThreadHtml(d)", FAKE)
        names = await page.evaluate(
            "() => Object.keys(BOT_NAME).map(k => k + '=' + BOT_NAME[k])")
        print("BOT_NAME:", names)
        print("mentions Ollama:", "Ollama" in html)
        print("mentions Gemini:", "Gemini" in html)
        await page.evaluate("""h => {
          const box = document.createElement('div');
          box.style.cssText = 'position:fixed;inset:0;z-index:99999;background:#0b0d10;padding:24px;overflow:auto';
          box.innerHTML = '<div style="color:#8b93a7;font:600 12px system-ui;letter-spacing:.08em;margin-bottom:14px">AI SECOND OPINION \\u00b7 rendered from a local Ollama</div>' + h;
          document.body.appendChild(box);
        }""", html)
        await page.wait_for_timeout(600)
        await page.screenshot(path=OUT)
        print("screenshot:", OUT)
        await b.close()


asyncio.run(main())
