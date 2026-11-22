"""Playwright reference script for the local 3-screen onboarding wizard benchmark (no submit)."""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

WIZARD_VERIFY_JS = """(code) => {
  return !document.getElementById('screen-3').classList.contains('hidden')
    && document.getElementById('summary-name').textContent === 'Taro Yamada'
    && document.getElementById('summary-email').textContent === 'taro@example.com'
    && document.getElementById('summary-code').textContent === code
    && document.getElementById('summary-token').textContent.startsWith('tok-')
    && document.getElementById('summary-workspace').textContent === 'core-app'
    && document.getElementById('summary-region').textContent === 'tokyo'
    && document.getElementById('summary-audit').textContent === 'enabled'
    && document.getElementById('note').value === 'Ready for staging review';
}"""


def main() -> None:
    chrome_bin = sys.argv[1]
    url = sys.argv[2]
    code = sys.argv[3] if len(sys.argv) > 3 else "INVITE-001"
    timeout_ms = int(sys.argv[4]) if len(sys.argv) > 4 else 10000

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome_bin, headless=True)
        page = browser.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto(url)
        page.get_by_label("Display name:").fill("Taro Yamada")
        page.get_by_label("Work email:").fill("taro@example.com")
        page.get_by_label("One-time code:").fill(code)
        page.get_by_role("button", name="Verify & continue").click()
        page.get_by_label("Workspace name:").fill("core-app")
        page.get_by_role("radio", name="Tokyo").check()
        page.get_by_role("checkbox", name="Enable audit log").check()
        page.get_by_role("button", name="Continue").click()
        page.get_by_label("Release note:").fill("Ready for staging review")
        ok = bool(page.evaluate(WIZARD_VERIFY_JS, code))
        browser.close()
        assert ok, "verify failed"


if __name__ == "__main__":
    main()
