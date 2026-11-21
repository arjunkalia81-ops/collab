"""One-time setup on a new machine: build a fresh browser_profile/ + clearance.json, then check plain HTTP gets past Cloudflare.

An existing browser_profile/ and clearance.json are deleted first, so the result always belongs to this machine.
Run on the worker VM itself (logged-in desktop, not over a headless session):
    python setup_profile.py
"""

import argparse
import asyncio
import logging
import shutil

import httpx
from playwright.async_api import Error as PlaywrightError

from worker_app.core.config import settings
from worker_app.playwright.browser import profile_dir
from worker_app.tn.clearance import FILE, CloudflareBlocked, clearance

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


async def plain_http_passes() -> bool:
    async with httpx.AsyncClient(
        headers={"User-Agent": clearance.user_agent, "Accept-Language": "en-US,en;q=0.9"},
        cookies=clearance.cookies,
        timeout=30,
    ) as client:
        response = await client.get(settings.therapynotes_login_url)
    print("Plain HTTP check:", response.status_code, response.headers.get("cf-mitigated") or "")
    return response.status_code == 200


async def main(wait_minutes: int) -> int:
    if settings.therapynotes_headless:
        print("THERAPYNOTES_HEADLESS is true in .env; set it to false, Cloudflare blocks headless Chrome.")
        return 1

    profile = profile_dir()
    if profile.exists():
        try:
            shutil.rmtree(profile)
        except OSError as exc:
            print(f"Could not delete {profile}: {exc}")
            print("Close any Chrome or worker using it and retry.")
            return 1
        print("Deleted old profile:", profile)
    if FILE.exists():
        FILE.unlink()
        print("Deleted old", FILE.name)

    print("Profile:", profile)
    print("A Chrome window will open. If Cloudflare shows a checkbox, click it. Don't close the window.")
    try:
        await clearance.refresh(force=True, wait_ms=wait_minutes * 60_000)
    except CloudflareBlocked as exc:
        print("Failed:", exc)
        return 1
    except PlaywrightError as exc:
        message = str(exc)
        if "not found" in message.lower() or "executable" in message.lower():
            print("Google Chrome not found. Install Chrome, or run: playwright install chrome")
        elif "user data directory is already in use" in message.lower():
            print("browser_profile is open in another Chrome (maybe the worker). Close it and retry.")
        else:
            print("Chrome failed:", message)
        return 1
    finally:
        await clearance.close()

    print("Saved:", FILE)
    print("User-Agent:", clearance.user_agent)
    if not await plain_http_passes():
        print("Plain HTTP is still blocked. Check that this VM's outbound IP isn't behind a proxy/VPN that changes.")
        return 1

    print("Done. Start the worker with: python browser_worker.py")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--wait", type=int, default=5, help="minutes to wait for the Cloudflare challenge (default 5)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.wait)))
