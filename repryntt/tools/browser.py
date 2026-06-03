"""
repryntt.tools.browser — Native browser control via Playwright.

Gives agents three capabilities without an MCP bridge:
    browse(url)        → page text + metadata  (headless GET)
    screenshot(url)    → PNG bytes saved to disk, path returned
    extract(url, sel)  → text from a CSS selector

Registers into the ToolRegistry under category "WEB".

Requires: ``pip install playwright && python -m playwright install chromium``
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_BROWSER_TIMEOUT = 30_000  # 30 s page load
_MAX_TEXT_LEN = 12_000     # truncate extracted text to keep context small


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return the running loop or create one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


async def _browse_async(url: str, *, timeout: int = _BROWSER_TIMEOUT) -> Dict[str, Any]:
    """Fetch a page and return its visible text + metadata."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            resp = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=timeout)
            title = await page.title()
            text = await page.inner_text("body")
            if len(text) > _MAX_TEXT_LEN:
                text = text[:_MAX_TEXT_LEN] + "\n…[truncated]"
            return {
                "url": url,
                "title": title,
                "status": resp.status if resp else None,
                "text": text,
            }
        finally:
            await browser.close()


async def _screenshot_async(
    url: str,
    dest: Path,
    *,
    full_page: bool = False,
    timeout: int = _BROWSER_TIMEOUT,
) -> Dict[str, Any]:
    """Capture a screenshot and write to *dest*."""
    from playwright.async_api import async_playwright

    dest.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 720})
            resp = await page.goto(url, wait_until="domcontentloaded",
                                   timeout=timeout)
            await page.screenshot(path=str(dest), full_page=full_page)
            title = await page.title()
            return {
                "url": url,
                "title": title,
                "status": resp.status if resp else None,
                "path": str(dest),
                "size_bytes": dest.stat().st_size,
            }
        finally:
            await browser.close()


async def _extract_async(
    url: str,
    selector: str,
    *,
    timeout: int = _BROWSER_TIMEOUT,
) -> Dict[str, Any]:
    """Extract text from elements matching *selector*."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded",
                            timeout=timeout)
            elements = await page.query_selector_all(selector)
            texts = []
            for el in elements[:50]:  # cap element count
                t = (await el.inner_text()).strip()
                if t:
                    texts.append(t)
            combined = "\n".join(texts)
            if len(combined) > _MAX_TEXT_LEN:
                combined = combined[:_MAX_TEXT_LEN] + "\n…[truncated]"
            return {
                "url": url,
                "selector": selector,
                "matches": len(texts),
                "text": combined,
            }
        finally:
            await browser.close()


# ── Sync wrappers (tool registry expects sync callables) ─────────────────

def browse(url: str, **_kw: Any) -> Dict[str, Any]:
    """Fetch *url* and return page text + metadata."""
    loop = _get_loop()
    if loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                lambda: asyncio.run(_browse_async(url))
            ).result(timeout=60)
    return loop.run_until_complete(_browse_async(url))


def screenshot(url: str, *, dest: Optional[str] = None, full_page: bool = False,
               **_kw: Any) -> Dict[str, Any]:
    """Capture a screenshot of *url*; returns ``{"path": ...}``."""
    if dest is None:
        from repryntt.paths import get_data_dir
        ts = int(time.time())
        dest_path = get_data_dir() / "screenshots" / f"shot_{ts}.png"
    else:
        dest_path = Path(dest)

    loop = _get_loop()
    if loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                lambda: asyncio.run(_screenshot_async(url, dest_path, full_page=full_page))
            ).result(timeout=60)
    return loop.run_until_complete(_screenshot_async(url, dest_path, full_page=full_page))


def extract(url: str, selector: str = "body", **_kw: Any) -> Dict[str, Any]:
    """Extract text matching *selector* from *url*."""
    loop = _get_loop()
    if loop.is_running():
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                lambda: asyncio.run(_extract_async(url, selector))
            ).result(timeout=60)
    return loop.run_until_complete(_extract_async(url, selector))


# ── Registry integration ─────────────────────────────────────────────────

def register(registry: Any) -> None:
    """Register browser tools into *registry* (a ToolRegistry instance)."""
    registry.register("browse", browse, category="WEB",
                      aliases=["browse_url", "web_browse"])
    registry.register("screenshot", screenshot, category="WEB",
                      aliases=["web_screenshot", "capture_page"])
    registry.register("extract", extract, category="WEB",
                      aliases=["web_extract", "css_select"])
    logger.info("Registered 3 browser tools (browse, screenshot, extract)")
