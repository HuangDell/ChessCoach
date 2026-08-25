from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


LEGACY_SELECTORS = (
    "#coach-ai-btn",
    "#set-coach-ai-auto",
    "#set-coach-ai-persist",
    "#set-local-llm-url",
    "#set-local-llm-model",
    "#set-ollama-detect",
    "#pz-explain",
    "#pz-chat-form",
    "#pz-storm-summary-btn",
)


def verify(base_url: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            desktop = browser.new_page(viewport={"width": 1440, "height": 900})
            desktop.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            desktop.on("pageerror", lambda error: errors.append(str(error)))
            response = desktop.goto(base_url, wait_until="networkidle")
            assert response is not None and response.ok, "Web root did not load"
            assert desktop.locator("#board").count() == 1
            assert desktop.locator("#clear-agent-runs").count() == 1
            for selector in LEGACY_SELECTORS:
                assert desktop.locator(selector).count() == 0, selector
            body_text = desktop.locator("body").inner_text().lower()
            assert "claude" not in body_text
            assert "snowie" not in body_text

            desktop.locator("#firstrun").evaluate("element => element.hidden = true")
            desktop.locator("#settings-toggle").click()
            desktop.locator("#settings").wait_for(state="visible")
            options = desktop.locator("#set-explanation-provider option").evaluate_all(
                "options => options.map(option => option.value)"
            )
            assert options == ["auto", "openai-compatible"]
            metrics = desktop.evaluate(
                "async () => { const r = await fetch('/api/agent/metrics?limit=10'); "
                "return {status: r.status, body: await r.json()}; }"
            )
            assert metrics["status"] == 200
            assert metrics["body"]["schema_version"] == 1
            desktop.screenshot(path=output_dir / "phase5-desktop.png", full_page=True)

            mobile = browser.new_page(viewport={"width": 390, "height": 844})
            mobile.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            mobile.on("pageerror", lambda error: errors.append(str(error)))
            response = mobile.goto(base_url, wait_until="networkidle")
            assert response is not None and response.ok
            mobile.locator("#firstrun").evaluate("element => element.hidden = true")
            overflow = mobile.evaluate(
                "() => ({width: document.documentElement.scrollWidth, viewport: innerWidth})"
            )
            assert overflow["width"] <= overflow["viewport"], overflow
            mobile.screenshot(path=output_dir / "phase5-mobile.png", full_page=True)
        finally:
            browser.close()
    assert not errors, "Browser errors: " + " | ".join(errors)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/chesscoach-phase5-ui"))
    args = parser.parse_args()
    verify(args.url, args.output_dir)
    print(f"Phase 5 Web UI verified; screenshots: {args.output_dir}")


if __name__ == "__main__":
    main()
