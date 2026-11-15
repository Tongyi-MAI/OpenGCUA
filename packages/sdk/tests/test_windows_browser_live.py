"""Opt-in headless Windows browser workflow through the public installed SDK.

Run `mu setup browser --engine chromium` first, then set
MAI_UI_RUN_BROWSER_LIVE=1. Managed Chromium matches the Windows harness's
isolated background factory; MAI_UI_BROWSER_LIVE_CHANNEL=chrome optionally
tests a separate headless system Chrome instance. Only a data URL and
fixture-owned tabs are used. No accounts, external pages or model calls.
"""

import asyncio
import json
import os
import sys
from urllib.parse import quote

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("MAI_UI_RUN_BROWSER_LIVE") != "1",
    reason="requires explicit opt-in, installed native SDK and browser worker",
)


HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Windows SDK fixture</title></head>
<body>
<h1>Release worksheet</h1>
<form id="release-form">
  <label>Version <input name="version" required></label>
  <label>Release date <input name="date" type="date" required></label>
  <button type="submit">Add release</button>
</form>
<table aria-label="Recorded releases">
  <thead><tr><th>Version</th><th>Release date</th></tr></thead><tbody></tbody>
</table>
<output aria-live="polite">0 rows saved</output>
<script>
document.querySelector('form').addEventListener('submit', event => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const row = document.querySelector('tbody').insertRow();
  for (const name of ['version', 'date']) {
    row.insertCell().textContent = form.get(name);
  }
  document.querySelector('output').textContent =
    `${document.querySelector('tbody').rows.length} rows saved`;
  event.currentTarget.reset();
});
</script></body></html>"""

ROWS = [["Fixture 1.0", "2024-01-15"], ["Fixture 2.0", "2025-03-20"]]


def test_headless_form_table_and_session_cleanup():
    from mai_ui import MuError
    from qwen_intelligence import Runtime

    channel = os.environ.get("MAI_UI_BROWSER_LIVE_CHANNEL", "chromium")
    assert channel in {"chrome", "chromium"}, "Use chrome or chromium for this isolated test"

    async def run():
        report = {"channel": channel, "headless": True, "background_launch": False}
        async with Runtime() as runtime:
            options = {"channel": "chrome"} if channel == "chrome" else {}
            # Exact Windows isolated-background harness settings. Never attach
            # to a user profile or pre-existing browser/CDP session.
            await runtime.add_browser_factory(
                "windows-fixture-browser",
                engine="chromium",
                headless=True,
                background_launch=False,
                viewport={"width": 960, "height": 720},
                **options,
            )
            async with runtime.session() as session:
                web = await session.browsers.create(
                    name="fixture-browser", provider="windows-fixture-browser", timeout_ms=60000
                )
                tab = await web.browser.new_tab(
                    "data:text/html;charset=utf-8," + quote(HTML), timeout_ms=30000
                )
                page = tab.playwright
                assert await page.title(timeout_ms=10000) == "Windows SDK fixture"
                receipts = []
                for index, (version, date) in enumerate(ROWS):
                    await page.get_by_label("Version", exact=True).fill(version, timeout_ms=10000)
                    await page.get_by_label("Release date", exact=True).fill(date, timeout_ms=10000)
                    action_id = f"fixture-submit-{index}"
                    await page.get_by_role("button", name="Add release", exact=True).click(
                        timeout_ms=10000, action_id=action_id
                    )
                    status = await session.action_status(action_id)
                    assert status["execution"] == "completed"
                    receipts.append({key: status.get(key) for key in ("execution", "effect")})

                assert await page.get_by_role("table", name="Recorded releases").locator(
                    "tbody tr"
                ).count(timeout_ms=10000) == len(ROWS)
                assert await page.get_by_text("2 rows saved", exact=True).is_visible(
                    timeout_ms=10000
                )
                # Independent read-back observes the browser's DOM after form
                # submission; it does not infer success from click receipts.
                actual = await page.evaluate(
                    "() => Array.from(document.querySelectorAll('tbody tr'), "
                    "row => Array.from(row.cells, cell => cell.textContent))",
                    timeout_ms=10000,
                )
                assert actual == ROWS
                assert (
                    await page.get_by_label("Version", exact=True).input_value(timeout_ms=10000)
                    == ""
                )

                tree = await tab.ui.ax.observe(timeout_ms=10000)
                assert any("Release worksheet" in node.name for node in tree.tree.nodes)
                png = await page.screenshot(timeout_ms=10000)
                assert png.startswith(b"\x89PNG\r\n\x1a\n")
                report.update(rows=actual, submit_receipts=receipts, screenshot_bytes=len(png))

                tab_id = tab.tab_id
                await tab.close(timeout_ms=10000)
                remaining = await web.browser.list_tabs(timeout_ms=10000)
                assert tab_id not in {item.id for item in remaining.tabs}
                report["explicit_tab_close_verified"] = True

                # Leave another owned tab open. Session.close must retire that
                # tab/context as part of its normal ownership cleanup.
                unfinished = await web.browser.new_tab(timeout_ms=10000)
                closed = await session.close(timeout_ms=15000)
                assert closed.queue_fenced and closed.in_flight_drained
                assert closed.unresolved_actions == ()
                with pytest.raises(MuError) as error:
                    await unfinished.playwright.title(timeout_ms=10000)
                assert error.value.code == "SESSION_CLOSED"
                report["session_cleanup"] = {
                    "queue_fenced": closed.queue_fenced,
                    "in_flight_drained": closed.in_flight_drained,
                    "unresolved_actions": list(closed.unresolved_actions),
                }
        print("BROWSER_LIVE_VERIFIED " + json.dumps(report, sort_keys=True))

    asyncio.run(asyncio.wait_for(run(), timeout=120))
