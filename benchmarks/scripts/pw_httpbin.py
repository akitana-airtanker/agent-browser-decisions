"""Playwright reference script for the 7-field httpbin form benchmark (no submit)."""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright

HTTPBIN_VERIFY_JS = """() => {
  const v = n => document.querySelector(`[name=${n}]`)?.value;
  const sz = document.querySelector('input[name=size]:checked')?.parentElement.textContent.trim();
  const tp = [...document.querySelectorAll('input[name=topping]:checked')].map(e => e.parentElement.textContent.trim());
  return v('custname') === 'Taro Yamada'
    && v('custtel') === '090-1234-5678'
    && v('custemail') === 'taro@example.com'
    && sz === 'Medium'
    && tp.length === 2 && tp.includes('Bacon') && tp.includes('Extra Cheese')
    && v('comments') === 'Leave at front door';
}"""


def main() -> None:
    chrome_bin = sys.argv[1]
    size = sys.argv[2] if len(sys.argv) > 2 else "Medium"
    timeout_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 10000

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chrome_bin, headless=True)
        page = browser.new_page()
        page.set_default_timeout(timeout_ms)
        page.goto("https://httpbin.org/forms/post")
        page.get_by_label("Customer name:").fill("Taro Yamada")
        page.get_by_label("Telephone:").fill("090-1234-5678")
        page.get_by_label("E-mail address:").fill("taro@example.com")
        page.get_by_role("radio", name=size).check()
        page.get_by_role("checkbox", name="Bacon").check()
        page.get_by_role("checkbox", name="Extra Cheese").check()
        page.get_by_label("Delivery instructions:").fill("Leave at front door")
        ok = bool(page.evaluate(HTTPBIN_VERIFY_JS))
        browser.close()
        assert ok, "verify failed"


if __name__ == "__main__":
    main()
