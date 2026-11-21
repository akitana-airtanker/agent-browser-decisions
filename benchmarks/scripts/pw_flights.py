"""Playwright reference script for the 12-step Google Flights one-way search benchmark.

Tuned for 5/5 stability against Google Flights DOM quirks:
- Wait for `tfs=` query update after choosing "One way"
- Target visible inputs/options directly (Google Flights keeps duplicate hidden comboboxes,
  trailing space in `Where to? `, and aria-hidden wrappers around autocomplete options)
- Select calendar date via `div[data-iso=...]:visible` and wait for Departure input reflection
- Wait for `[role="dialog"]:visible` to disappear before clicking Search, with 1 retry on re-render
"""

from __future__ import annotations

import re
import sys

from playwright.sync_api import TimeoutError as PwTimeout
from playwright.sync_api import sync_playwright

FLIGHTS_URL = "https://www.google.com/travel/flights?hl=en"

FLIGHTS_VERIFY_JS = """() => {
  if (!location.href.includes('/travel/flights/search')) return false;
  const val = name => [...document.querySelectorAll('input')]
    .filter(e => e.offsetParent !== null && (e.getAttribute('aria-label') || '').trim() === name)
    .map(e => e.value)[0] || '';
  const retVisible = [...document.querySelectorAll('input[aria-label="Return"]')]
    .some(e => e.offsetParent !== null);
  return val('Where from?').includes('Zürich')
    && val('Where to?').includes('London')
    && val('Departure').includes('Dec 15')
    && !retVisible;
}"""


def main() -> None:
    chrome_bin = sys.argv[1]
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chrome_bin,
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_context(locale="en-US").new_page()
        page.set_default_timeout(10000)
        page.goto(FLIGHTS_URL)

        page.get_by_role("combobox", name=re.compile(r"^Change ticket type\.")).click()
        page.get_by_role("option", name="One way").click()
        page.wait_for_function("() => location.search.includes('tfs=')")

        page.locator('input[aria-label^="Where from?"]:visible').first.click()
        from_inp = page.locator('input[aria-label="Where else?"]:visible').first
        from_inp.wait_for(state="visible")
        from_inp.fill("Zurich")
        zrh = page.locator('[role="option"][aria-label="Zürich, Switzerland"]:visible').first
        zrh.click()
        zrh.wait_for(state="hidden")

        page.locator('input[aria-label^="Where to?"]:visible').first.click()
        to_inp = page.locator('input[aria-label^="Where to?"]:visible').last
        to_inp.wait_for(state="visible")
        to_inp.fill("London")
        lon = page.locator('[role="option"][aria-label="London, United Kingdom"]:visible').first
        lon.click()
        lon.wait_for(state="hidden")

        page.locator('input[aria-label="Departure"]:visible').first.click()
        page.locator('div[data-iso="2026-12-15"]:visible').first.click()
        page.wait_for_function(
            """() => [...document.querySelectorAll('input[aria-label="Departure"]')]
                .some(e => (e.value || '').includes('Dec 15'))"""
        )
        done_btn = page.locator('button[aria-label^="Done."]:visible').first
        done_btn.click()
        page.locator('[role="dialog"]:visible').wait_for(state="hidden")

        for attempt in range(2):
            page.locator('button[aria-label="Search"]:visible').first.click()
            try:
                page.wait_for_function(FLIGHTS_VERIFY_JS, timeout=4000 if attempt == 0 else 8000)
                break
            except PwTimeout:
                if attempt == 1:
                    raise

        ok = bool(page.evaluate(FLIGHTS_VERIFY_JS))
        browser.close()
        assert ok, f"verify failed: {page.url}"


if __name__ == "__main__":
    main()
