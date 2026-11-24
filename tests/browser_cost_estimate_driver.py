"""Check that navigation and budget refresh use only the current purchasing evidence."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import browser_smoke as harness  # noqa: E402

OUTPUT = ROOT / ".local/proposal-release/cost-driver"


def serve():
    import browser_foundation_server as foundation
    import uvicorn
    from test_design_workflow import hardware
    from test_microplates import select
    from test_price_estimation import declare

    import app.api as api
    from app.design_workflow import refresh_workflow

    initial = declare(refresh_workflow(select(hardware())), "upstream-platform")
    api.new_project = lambda: deepcopy(initial)
    args = foundation.baseline.parse_args()
    uvicorn.run(api.app, host="127.0.0.1", port=args.port, log_level="warning")


def run(url):
    from playwright.sync_api import expect, sync_playwright

    OUTPUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        posts = []
        page.on("request", lambda req: posts.append(req.url)
                if "/cost-estimate" in req.url and req.method == "POST" else None)
        page.goto(url, wait_until="networkidle")
        nav = page.get_by_role("navigation", name="Project workspace", exact=True)
        nav.get_by_role("button", name="Parts & prices", exact=True).click()
        summary = page.locator(".parts-total")
        expect(summary).to_contain_text("Documented prices subtotal")
        expect(summary).to_contain_text("$80–$120")
        initial = summary.inner_text()
        refresh = page.get_by_role("button", name="Refresh cost breakdown", exact=True)
        with page.expect_response(lambda r: "/cost-estimate?" in r.url) as response:
            refresh.click()
        assert response.value.ok
        expect(summary).to_have_text(initial, use_inner_text=True)
        nav.get_by_role("button", name="3D design", exact=True).click()
        nav.get_by_role("button", name="Parts & prices", exact=True).click()
        expect(summary).to_have_text(initial, use_inner_text=True)
        assert not posts, posts
        page.screenshot(path=str(OUTPUT / "classified-costs.png"))
        (OUTPUT / "checks.json").write_text(
            json.dumps({"summary": initial, "price_model_requests": posts}, indent=2),
            encoding="utf-8",
        )
        browser.close()


if __name__ == "__main__":
    if "--port" in sys.argv:
        serve()
    else:
        harness.STUB_SERVER = Path(__file__).resolve()
        harness.run_smoke = run
        raise SystemExit(harness.main())
