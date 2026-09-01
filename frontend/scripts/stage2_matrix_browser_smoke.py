from __future__ import annotations

import os
import re
from pathlib import Path

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
SCREENSHOT_ROOT = ROOT.parent / ".codex-artifacts"
MATRIX_URL = os.environ.get(
    "STAGE2_MATRIX_URL", "http://127.0.0.1:18088/evaluation/stage2-matrices"
)


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        headless=True,
        executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    page = browser.new_page(viewport={"width": 1680, "height": 1100})
    console_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.goto(MATRIX_URL)
    page.wait_for_load_state("networkidle")
    page.get_by_role("heading", name="Stage2 实验矩阵审计").wait_for()
    page.get_by_text("实验执行完整，但目前没有可计分的 PASS/FAIL").wait_for()
    page.get_by_text("9/9 manifests").wait_for()
    page.get_by_role(
        "button",
        name="deepseek-harness claude-opus-5 D4 CASE_INVALID",
    ).click()
    page.get_by_text("混沌工程实验总结报告").wait_for()
    page.get_by_role("tab", name=re.compile(r"^Controller 动作 \(\d+\)$")).click()
    page.get_by_text("trial_finished", exact=True).wait_for()
    page.get_by_role("tab", name="独立 Oracle / 恢复").click()
    page.get_by_text("故障窗口延迟", exact=True).wait_for()
    SCREENSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(SCREENSHOT_ROOT / "stage2-matrix-trial-evidence.png"), full_page=True)
    page.locator(".ant-drawer-close").click()
    page.wait_for_timeout(500)
    page.screenshot(path=str(SCREENSHOT_ROOT / "stage2-matrix-overview.png"), full_page=True)
    unexpected_errors = [
        item for item in console_errors if not item.startswith("Warning: [antd:")
    ]
    assert not unexpected_errors, unexpected_errors
    print(
        "browser_smoke=passed "
        f"overview={SCREENSHOT_ROOT / 'stage2-matrix-overview.png'} "
        f"trial={SCREENSHOT_ROOT / 'stage2-matrix-trial-evidence.png'}"
    )
    browser.close()
