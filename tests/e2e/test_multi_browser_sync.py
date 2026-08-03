import asyncio
import os
from uuid import uuid4

import pytest

playwright = pytest.importorskip("playwright.async_api")
async_playwright = playwright.async_playwright
expect = playwright.expect

BASE_URL = os.getenv("E2E_BASE_URL")
pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="Set E2E_BASE_URL to run browser tests against a started stack",
)


@pytest.mark.asyncio
async def test_owner_tabs_reconcile_visibility_and_public_link() -> None:
    assert BASE_URL is not None
    username = f"owner-{uuid4().hex[:10]}"
    email = f"{username}@example.com"
    password = "browser-test-password-123"

    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch()
        first_context = await browser.new_context(
            permissions=["clipboard-read", "clipboard-write"]
        )
        second_context = await browser.new_context(
            permissions=["clipboard-read", "clipboard-write"]
        )
        first = await first_context.new_page()
        second = await second_context.new_page()
        try:
            for _attempt in range(40):
                try:
                    health = await first.request.get(f"{BASE_URL}/api/health")
                    if health.ok:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            else:
                pytest.fail(f"Server did not become ready: {BASE_URL}")
            await first.goto(BASE_URL)
            await first.locator("#show-register").click()
            await first.locator("#register-email").fill(email)
            await first.locator("#register-username").fill(username)
            await first.locator("#register-password").fill(password)
            await first.locator("#register-form button[type=submit]").click()
            await first.locator("#current-user").wait_for()

            await first.locator("#create-visibility").select_option("private")
            await first.locator("#create-session").click()
            await expect(first.locator("#session-access")).to_have_text("owner · private")

            await second.goto(BASE_URL)
            await second.locator("#login-identifier").fill(username)
            await second.locator("#login-password").fill(password)
            await second.locator("#login-form button[type=submit]").click()
            await second.locator(".session-card").first.click()
            await expect(second.locator("#session-access")).to_have_text("owner · private")

            await first.locator("#session-visibility").select_option("public")
            await expect(second.locator("#public-link-panel")).to_be_visible()
            public_url = await second.locator("#public-link").input_value()
            assert public_url.startswith(BASE_URL)
            assert "/watch/" in public_url

            await second.locator("#copy-public-link").click()
            await expect(second.locator("#copy-public-link")).to_have_text("Đã copy")

            await first.locator("#session-visibility").select_option("private")
            await expect(second.locator("#public-link-panel")).to_be_hidden()
        finally:
            await first_context.close()
            await second_context.close()
            await browser.close()
