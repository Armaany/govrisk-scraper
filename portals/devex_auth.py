# Playwright authentication utilities for Devex login and session reuse.
import json
import random
from pathlib import Path
from playwright.async_api import Browser, BrowserContext, Page, async_playwright


from config import Config
class AuthenticationError(Exception):
    """Raised when Devex authentication cannot be completed successfully."""


class DevexAuth:
    """Handles login and session restoration for Devex using Playwright."""

    def __init__(self, config: Config):
        """Store config and initialize Playwright browser resources."""
        self.config = config
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    async def _start_browser(self) -> None:
        """Start Playwright and create a browser/context/page if needed."""
        if self.browser and self.context and self.page:
            return
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=self.config.headless)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

    async def _is_logged_in(self, page: Page) -> bool:
        """Return True when dashboard URL or avatar element indicates auth success."""
        if "devex.com/dashboard" in page.url:
            return True
        avatar_selector = '[data-testid="user-avatar"], .user-avatar, img[alt*="avatar" i]'
        return await page.locator(avatar_selector).count() > 0

    async def login(self) -> Page:
        """Log into Devex, persist cookies, and return an authenticated page."""
        await self._start_browser()
        assert self.page is not None
        assert self.context is not None
        page = self.page# mask Playwright from bot detection
        try:
            await page.wait_for_timeout(random.randint(1500, 3000))  # human-like pause
            await page.goto("https://www.devex.com/login", wait_until="domcontentloaded")
            await page.wait_for_selector('input[type="email"], input[name="email"]', timeout=20000)
            await page.fill('input[type="email"], input[name="email"]', self.config.devex_email)
            await page.fill('input[type="password"], input[name="password"]', self.config.devex_password)
            await page.click('button[type="submit"], button:has-text("Log in"), button:has-text("Login")')

            try:
                await page.wait_for_url("**/dashboard**", timeout=30000)
            except Exception:
                await page.wait_for_timeout(3000)

            if not await self._is_logged_in(page):
                raise AuthenticationError("Devex login failed — check DEVEX_EMAIL and DEVEX_PASSWORD")

            session_path = Path(self.config.devex_session_path)
            session_path.parent.mkdir(parents=True, exist_ok=True)
            cookies = await self.context.cookies()
            session_path.write_text(json.dumps(cookies, indent=2), encoding="utf-8")
            return page
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError("Devex login failed — check DEVEX_EMAIL and DEVEX_PASSWORD") from exc

    async def load_session(self) -> Page:
        """Load saved session cookies when available; otherwise perform fresh login."""
        await self._start_browser()
        assert self.page is not None
        assert self.context is not None
        page = self.page

        session_path = Path(self.config.devex_session_path)
        if not session_path.exists():
            return await self.login()

        try:
            raw = session_path.read_text(encoding="utf-8")
            cookies = json.loads(raw)
            if isinstance(cookies, list) and cookies:
                await self.context.add_cookies(cookies)
            await page.goto("https://www.devex.com", wait_until="domcontentloaded")
            if await self._is_logged_in(page):
                return page
            return await self.login()
        except Exception:
            return await self.login()

    async def close(self):
        """Close Playwright resources cleanly."""
        if self.context is not None:
            await self.context.close()
            self.context = None
        if self.browser is not None:
            await self.browser.close()
            self.browser = None
        if self.playwright is not None:
            await self.playwright.stop()
            self.playwright = None
        self.page = None
