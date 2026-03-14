"""
SessionKeeper — Browser session manager for automation that handles CAPTCHA gracefully.

When automation hits a login wall or CAPTCHA:
1. Opens a visible browser window for human intervention
2. Waits for the human to solve it
3. Saves the authenticated session
4. Returns to headless automation

Usage:
    from sessionkeeper import SessionKeeper

    async with SessionKeeper("reddit") as sk:
        page = await sk.get_authenticated_page("https://reddit.com")
        # page is already logged in — do your automation
        await page.goto("https://reddit.com/r/blender/submit")

CLI:
    python sessionkeeper.py auth reddit --url https://reddit.com/login
    python sessionkeeper.py auth gumroad --url https://app.gumroad.com/login
    python sessionkeeper.py status
    python sessionkeeper.py clear reddit

How it works:
    - Sessions stored as Playwright storage_state JSON files
    - Health checks verify session validity before returning
    - If session expired → opens visible browser for re-auth
    - Configurable check URLs and success conditions
    - Works with any site — just define the auth config
"""

import asyncio
import json
import os
import sys
import time
import logging
import argparse
from pathlib import Path
from datetime import datetime, timedelta

try:
    from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("pip install playwright && playwright install firefox")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("sessionkeeper")

# Default session storage directory
DEFAULT_SESSION_DIR = Path.home() / ".sessionkeeper"

# Built-in site configurations
SITE_CONFIGS = {
    "reddit": {
        "login_url": "https://www.reddit.com/login/",
        "check_url": "https://old.reddit.com",
        "success_indicator": "span.user-name, a[href*='/user/']",
        "failure_indicator": "input[name='password']",
        "display_name": "Reddit",
    },
    "gumroad": {
        "login_url": "https://app.gumroad.com/login",
        "check_url": "https://app.gumroad.com/dashboard",
        "success_indicator": "a[href*='products'], a[href*='dashboard']",
        "failure_indicator": "input[type='password'], button:has-text('Login')",
        "display_name": "Gumroad",
    },
    "devto": {
        "login_url": "https://dev.to/enter",
        "check_url": "https://dev.to/dashboard",
        "success_indicator": "a[href*='dashboard']",
        "failure_indicator": "a:has-text('Log in')",
        "display_name": "DEV.to",
    },
    "twitter": {
        "login_url": "https://twitter.com/i/flow/login",
        "check_url": "https://twitter.com/home",
        "success_indicator": "a[href='/compose/tweet'], a[aria-label*='Post']",
        "failure_indicator": "input[name='text']",
        "display_name": "X (Twitter)",
    },
    "note": {
        "login_url": "https://note.com/login",
        "check_url": "https://note.com/dashboard",
        "success_indicator": "a[href*='dashboard']",
        "failure_indicator": "input[type='password']",
        "display_name": "note.com",
    },
}


class SessionKeeper:
    """Manages browser sessions with automatic CAPTCHA/login handling."""

    def __init__(self, site_name, session_dir=None, config=None, browser_type="firefox"):
        self.site_name = site_name
        self.session_dir = Path(session_dir or DEFAULT_SESSION_DIR)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.session_path = self.session_dir / f"{site_name}_session.json"
        self.meta_path = self.session_dir / f"{site_name}_meta.json"
        self.browser_type = browser_type

        # Use built-in config or custom
        if config:
            self.config = config
        elif site_name in SITE_CONFIGS:
            self.config = SITE_CONFIGS[site_name]
        else:
            raise ValueError(
                f"Unknown site '{site_name}'. Use one of {list(SITE_CONFIGS.keys())} "
                f"or provide a custom config dict."
            )

        self._playwright_cm = None
        self._playwright = None
        self._browser = None
        self._context = None

    async def __aenter__(self):
        self._playwright_cm = async_playwright()
        self._playwright = await self._playwright_cm.start()
        return self

    async def __aexit__(self, *args):
        if self._context:
            try: await self._context.close()
            except: pass
        if self._browser:
            try: await self._browser.close()
            except: pass
        if self._playwright_cm:
            await self._playwright_cm.__aexit__(*args)

    def _get_browser_launcher(self):
        """Get the appropriate browser launcher."""
        if self.browser_type == "firefox":
            return self._playwright.firefox
        elif self.browser_type == "chromium":
            return self._playwright.chromium
        elif self.browser_type == "webkit":
            return self._playwright.webkit
        return self._playwright.firefox

    async def _launch_browser(self, headless=True):
        """Launch browser with common settings."""
        launcher = self._get_browser_launcher()
        return await launcher.launch(headless=headless)

    async def _create_context(self, browser, use_session=True):
        """Create a browser context, optionally loading saved session."""
        kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
        }
        if use_session and self.session_path.exists():
            kwargs["storage_state"] = str(self.session_path)

        return await browser.new_context(**kwargs)

    async def check_session(self):
        """Check if the saved session is still valid. Returns True/False."""
        if not self.session_path.exists():
            logger.info(f"[{self.site_name}] No saved session found")
            return False

        browser = await self._launch_browser(headless=True)
        try:
            context = await self._create_context(browser, use_session=True)
            page = await context.new_page()

            await page.goto(self.config["check_url"], wait_until="domcontentloaded", timeout=20000)
            await page.wait_for_timeout(3000)

            # Check for success indicator
            success = page.locator(self.config["success_indicator"]).first
            if await success.count() > 0:
                logger.info(f"[{self.site_name}] Session valid")
                await context.close()
                return True

            logger.info(f"[{self.site_name}] Session expired")
            await context.close()
            return False

        except Exception as e:
            logger.warning(f"[{self.site_name}] Session check failed: {e}")
            return False
        finally:
            await browser.close()

    async def authenticate(self, timeout_minutes=5):
        """
        Open a visible browser for the user to log in manually.
        Waits for authentication, then saves the session.

        Returns True if authentication succeeded.
        """
        logger.info(f"[{self.site_name}] Opening browser for authentication...")
        logger.info(f"[{self.site_name}] Please log in within {timeout_minutes} minutes")
        logger.info(f"[{self.site_name}] The browser will close automatically after login")

        browser = await self._launch_browser(headless=False)  # VISIBLE browser
        try:
            context = await self._create_context(browser, use_session=False)
            page = await context.new_page()

            # Navigate to login page
            await page.goto(self.config["login_url"], wait_until="domcontentloaded", timeout=30000)

            # Wait for the user to complete login
            deadline = time.time() + (timeout_minutes * 60)
            authenticated = False

            print(f"\n{'='*60}")
            print(f"  SessionKeeper: {self.config['display_name']}")
            print(f"  Please log in in the browser window.")
            print(f"  Solve any CAPTCHAs if prompted.")
            print(f"  The window will close automatically when done.")
            print(f"{'='*60}\n")

            while time.time() < deadline:
                await page.wait_for_timeout(2000)

                try:
                    # Check if we're past the login page
                    current_url = page.url
                    failure = page.locator(self.config.get("failure_indicator", "nonexistent")).first

                    # Check success indicator on the current page or navigate to check URL
                    if "login" not in current_url.lower():
                        # Navigate to check URL to verify
                        await page.goto(self.config["check_url"], wait_until="domcontentloaded", timeout=15000)
                        await page.wait_for_timeout(2000)

                        success = page.locator(self.config["success_indicator"]).first
                        if await success.count() > 0:
                            authenticated = True
                            break

                except PlaywrightTimeout:
                    continue
                except Exception as e:
                    logger.debug(f"Check iteration error: {e}")
                    continue

            if authenticated:
                # Save session
                await context.storage_state(path=str(self.session_path))
                self._save_meta({
                    "site": self.site_name,
                    "authenticated_at": datetime.now().isoformat(),
                    "display_name": self.config["display_name"],
                })
                logger.info(f"[{self.site_name}] Session saved to {self.session_path}")
                print(f"\n  Session saved successfully!\n")
            else:
                logger.warning(f"[{self.site_name}] Authentication timed out")
                print(f"\n  Authentication timed out. Please try again.\n")

            await context.close()
            return authenticated

        finally:
            await browser.close()

    async def get_authenticated_page(self, url=None, headless=True):
        """
        Get an authenticated browser page ready for automation.

        If session is invalid, opens visible browser for re-auth.
        Returns (context, page) tuple.
        """
        # Check existing session
        is_valid = await self.check_session()

        if not is_valid:
            # Need to authenticate
            logger.info(f"[{self.site_name}] Session invalid, requesting authentication...")
            success = await self.authenticate()
            if not success:
                raise RuntimeError(f"Authentication failed for {self.site_name}")

        # Create headless context with valid session
        self._browser = await self._launch_browser(headless=headless)
        self._context = await self._create_context(self._browser, use_session=True)
        page = await self._context.new_page()

        if url:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

        return page

    async def save_session(self):
        """Save current context's session state."""
        if self._context:
            await self._context.storage_state(path=str(self.session_path))
            logger.info(f"[{self.site_name}] Session updated")

    def _save_meta(self, data):
        """Save session metadata."""
        with open(self.meta_path, "w") as f:
            json.dump(data, f, indent=2)

    def get_status(self):
        """Get session status info."""
        if not self.session_path.exists():
            return {"site": self.site_name, "status": "no_session"}

        meta = {}
        if self.meta_path.exists():
            with open(self.meta_path) as f:
                meta = json.load(f)

        stat = self.session_path.stat()
        age_hours = (time.time() - stat.st_mtime) / 3600

        return {
            "site": self.site_name,
            "status": "saved",
            "session_file": str(self.session_path),
            "age_hours": round(age_hours, 1),
            "authenticated_at": meta.get("authenticated_at", "unknown"),
            "display_name": meta.get("display_name", self.site_name),
        }

    def clear_session(self):
        """Delete saved session."""
        if self.session_path.exists():
            self.session_path.unlink()
        if self.meta_path.exists():
            self.meta_path.unlink()
        logger.info(f"[{self.site_name}] Session cleared")


# ============================================================
# CLI Interface
# ============================================================

async def cli_auth(args):
    """Authenticate with a site."""
    config = None
    if args.url:
        # Custom site config
        config = {
            "login_url": args.url,
            "check_url": args.check_url or args.url.rsplit("/", 1)[0],
            "success_indicator": args.success or "a[href*='dashboard']",
            "failure_indicator": args.failure or "input[type='password']",
            "display_name": args.site,
        }

    async with SessionKeeper(args.site, config=config) as sk:
        success = await sk.authenticate(timeout_minutes=args.timeout)
        if success:
            print(f"Authenticated with {args.site}")
            # Verify
            valid = await sk.check_session()
            print(f"Session verification: {'PASS' if valid else 'FAIL'}")
        else:
            print(f"Authentication failed for {args.site}")
            sys.exit(1)


async def cli_check(args):
    """Check session validity."""
    async with SessionKeeper(args.site) as sk:
        valid = await sk.check_session()
        status = sk.get_status()
        print(f"\n{status['display_name']}:")
        print(f"  Status: {'valid' if valid else 'expired/missing'}")
        print(f"  Age: {status.get('age_hours', 'N/A')} hours")
        print(f"  File: {status.get('session_file', 'N/A')}")


async def cli_status(args):
    """Show status of all sessions."""
    print("\nSessionKeeper — Saved Sessions")
    print("=" * 50)

    session_dir = Path(DEFAULT_SESSION_DIR)
    if not session_dir.exists():
        print("No sessions found.")
        return

    for name in sorted(SITE_CONFIGS.keys()):
        sk = SessionKeeper(name)
        status = sk.get_status()
        if status["status"] == "saved":
            age = status.get("age_hours", "?")
            auth_at = status.get("authenticated_at", "unknown")
            print(f"  {status['display_name']:15s} | {age:>6}h ago | auth: {auth_at}")
        else:
            display = SITE_CONFIGS[name]["display_name"]
            print(f"  {display:15s} | no session")

    # Check for custom sessions
    for f in session_dir.glob("*_session.json"):
        name = f.stem.replace("_session", "")
        if name not in SITE_CONFIGS:
            stat = f.stat()
            age = round((time.time() - stat.st_mtime) / 3600, 1)
            print(f"  {name:15s} | {age:>6}h ago | (custom)")

    print()


async def cli_clear(args):
    """Clear a session."""
    sk = SessionKeeper(args.site)
    sk.clear_session()
    print(f"Session cleared for {args.site}")


def main():
    parser = argparse.ArgumentParser(
        description="SessionKeeper — Browser session manager for automation"
    )
    subparsers = parser.add_subparsers(dest="command")

    # auth
    auth_parser = subparsers.add_parser("auth", help="Authenticate with a site")
    auth_parser.add_argument("site", type=str, help="Site name (reddit, gumroad, etc.)")
    auth_parser.add_argument("--url", type=str, help="Custom login URL")
    auth_parser.add_argument("--check-url", type=str, help="URL to check after login")
    auth_parser.add_argument("--success", type=str, help="CSS selector for success")
    auth_parser.add_argument("--failure", type=str, help="CSS selector for failure")
    auth_parser.add_argument("--timeout", type=int, default=5, help="Timeout in minutes")

    # check
    check_parser = subparsers.add_parser("check", help="Check session validity")
    check_parser.add_argument("site", type=str)

    # status
    subparsers.add_parser("status", help="Show all session statuses")

    # clear
    clear_parser = subparsers.add_parser("clear", help="Clear a session")
    clear_parser.add_argument("site", type=str)

    args = parser.parse_args()

    if args.command == "auth":
        asyncio.run(cli_auth(args))
    elif args.command == "check":
        asyncio.run(cli_check(args))
    elif args.command == "status":
        asyncio.run(cli_status(args))
    elif args.command == "clear":
        asyncio.run(cli_clear(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
