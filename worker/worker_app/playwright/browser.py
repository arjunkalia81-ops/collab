"""Launch / close real Chrome with a persistent profile. Used only to refresh the Cloudflare clearance."""

import logging
from pathlib import Path

from playwright.async_api import async_playwright

from worker_app.core.config import settings

log = logging.getLogger(__name__)


def profile_dir() -> Path:
    path = Path(settings.browser_profile)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


async def open_browser():
    playwright = await async_playwright().start()

    # Prefer an already-running local Chrome (user started with --remote-debugging-port).
    cdp = (settings.chrome_cdp_url or "").strip()
    if cdp:
        log.info("Connecting to local Chrome via CDP: %s", cdp)
        browser = await playwright.chromium.connect_over_cdp(cdp)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        return playwright, context, page

    launch = {
        "user_data_dir": str(profile_dir()),
        "headless": settings.therapynotes_headless,
        # Playwright's defaults (--no-sandbox, --enable-automation) are bot signals Cloudflare checks for.
        "chromium_sandbox": True,
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
        "viewport": {"width": 1280, "height": 800},
    }
    exe = (settings.chrome_executable or "").strip()
    if exe:
        launch["executable_path"] = exe
        log.info("Local Chrome: %s (profile=%s, headless=%s)", exe, profile_dir(), settings.therapynotes_headless)
    else:
        launch["channel"] = "chrome"
        log.info("Local Chrome channel=chrome (profile=%s, headless=%s)", profile_dir(), settings.therapynotes_headless)

    context = await playwright.chromium.launch_persistent_context(**launch)
    await context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    page = context.pages[0] if context.pages else await context.new_page()
    return playwright, context, page


async def close_browser(playwright, context) -> None:
    # CDP-attached contexts must not be closed — that would quit the user's Chrome.
    cdp = (settings.chrome_cdp_url or "").strip()
    try:
        if context and not cdp:
            await context.close()
    except Exception as exc:
        log.warning("context.close: %s %s", type(exc).__name__, exc)
    try:
        if playwright:
            await playwright.stop()
    except Exception as exc:
        log.warning("playwright.stop: %s %s", type(exc).__name__, exc)
