from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlencode

if TYPE_CHECKING:
    from playwright.async_api import Page

PreviewLevel = Literal["guest", "user", "sudo", "root"]
PreviewAction = Literal["sim_upload", "sim_download", "clear_transfers"]
_DEFAULT_CHROMIUM_ARGS: tuple[str, ...] = ("--no-sandbox", "--disable-dev-shm-usage")


@dataclass(frozen=True, slots=True)
class PreviewConfig:
    base_url: str
    path: str
    level: PreviewLevel
    screenshot_path: Path
    executable_path: str | None
    viewport_width: int
    viewport_height: int
    wait_after_load_ms: int
    wait_after_actions_ms: int
    full_page: bool
    headless: bool
    actions: tuple[PreviewAction, ...]


def _parse_args() -> PreviewConfig:
    parser = argparse.ArgumentParser(
        description="Open a local mod web page, sign in via dev auth, optionally trigger tray actions, and save a screenshot."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:3180", help="Base URL for the running dashboard.")
    parser.add_argument(
        "--path",
        default="/mod-web",
        help="Path to open after dev-login, such as /mod-web or /mod-web/mods/sevendays_alpha.",
    )
    parser.add_argument(
        "--level",
        choices=("guest", "user", "sudo", "root"),
        default="root",
        help="Dev-login level to use.",
    )
    parser.add_argument(
        "--screenshot",
        default="tmp/mod-web-preview.png",
        help="Output screenshot path.",
    )
    parser.add_argument(
        "--executable-path",
        default=None,
        help="Optional browser executable path. Defaults to a detected local Chrome/Chromium binary when available.",
    )
    parser.add_argument("--viewport-width", type=int, default=1600, help="Browser viewport width in pixels.")
    parser.add_argument("--viewport-height", type=int, default=1000, help="Browser viewport height in pixels.")
    parser.add_argument("--wait-after-load-ms", type=int, default=900, help="Wait after initial page load.")
    parser.add_argument(
        "--wait-after-actions-ms",
        type=int,
        default=700,
        help="Wait after menu actions before taking the screenshot.",
    )
    parser.add_argument("--full-page", action="store_true", help="Capture a full-page screenshot.")
    parser.add_argument("--headed", action="store_true", help="Run with a visible browser window.")
    parser.add_argument(
        "--action",
        action="append",
        choices=("sim_upload", "sim_download", "clear_transfers"),
        default=[],
        help="Optional utility-menu action to run. Repeat to queue multiple actions.",
    )
    args = parser.parse_args()
    return PreviewConfig(
        base_url=args.base_url.rstrip("/"),
        path=args.path if args.path.startswith("/") else f"/{args.path}",
        level=args.level,
        screenshot_path=Path(args.screenshot),
        executable_path=_resolve_browser_executable_path(args.executable_path),
        viewport_width=args.viewport_width,
        viewport_height=args.viewport_height,
        wait_after_load_ms=args.wait_after_load_ms,
        wait_after_actions_ms=args.wait_after_actions_ms,
        full_page=bool(args.full_page),
        headless=not bool(args.headed),
        actions=tuple(args.action),
    )


def _resolve_browser_executable_path(raw_value: str | None) -> str | None:
    if raw_value is not None:
        value = raw_value.strip()
        return value or None
    for candidate in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser", "chrome"):
        resolved = which(candidate)
        if resolved:
            return resolved
    return None


def _dev_login_url(config: PreviewConfig) -> str:
    query = urlencode({"level": config.level, "next_path": config.path})
    return f"{config.base_url}/auth/dev-login?{query}"


async def _click_utility_action(*, page: "Page", action: PreviewAction) -> None:
    await page.get_by_role("button", name="Utilities").click()
    await page.get_by_text("Tray Tools").wait_for()
    button_name_by_action: dict[PreviewAction, str] = {
        "sim_upload": "Sim Upload",
        "sim_download": "Sim Download",
        "clear_transfers": "Clear Transfers",
    }
    await page.get_by_role("button", name=button_name_by_action[action]).click()


async def _wait_for_preview_ready(*, page: "Page", requires_utility_menu: bool) -> None:
    await page.locator("body").first.wait_for()
    if requires_utility_menu:
        await page.get_by_role("button", name="Utilities").wait_for()


async def _run_preview(config: PreviewConfig) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as xcp:
        raise RuntimeError(
            "Playwright is not installed. Run `uv sync` and `uv run python -m playwright install chromium` first."
        ) from xcp

    config.screenshot_path.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        launch_kwargs: dict[str, object] = {"headless": config.headless}
        if config.executable_path is not None:
            launch_kwargs["executable_path"] = config.executable_path
        launch_kwargs["args"] = list(_DEFAULT_CHROMIUM_ARGS)
        browser = await playwright.chromium.launch(**launch_kwargs)
        try:
            page = await browser.new_page(
                viewport={"width": config.viewport_width, "height": config.viewport_height},
                device_scale_factor=1,
            )
            await page.goto(_dev_login_url(config), wait_until="domcontentloaded")
            await page.wait_for_timeout(config.wait_after_load_ms)
            await _wait_for_preview_ready(page=page, requires_utility_menu=bool(config.actions))

            for action in config.actions:
                await _click_utility_action(page=page, action=action)
                await page.wait_for_timeout(250)

            if config.actions:
                await page.wait_for_timeout(config.wait_after_actions_ms)

            await page.screenshot(path=str(config.screenshot_path), full_page=config.full_page)
            print(f"Saved screenshot to {config.screenshot_path}")
            print(f"Final URL: {page.url}")
        finally:
            await browser.close()


def main() -> None:
    config = _parse_args()
    asyncio.run(_run_preview(config))


if __name__ == "__main__":
    main()
