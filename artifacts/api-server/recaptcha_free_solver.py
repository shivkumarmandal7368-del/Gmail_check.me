"""Orchestrate free captcha paths: audio (Whisper) then vision (YOLO)."""

from __future__ import annotations

import time
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver

from recaptcha_audio_solver import free_audio_enabled, try_solve_recaptcha_audio_page
from recaptcha_image_solver import free_vision_enabled, try_solve_recaptcha_image_page


def _log(msg: str) -> None:
    print(f"[UC] [captcha-free] {msg}", flush=True)


def _wait_recaptcha_cleared(
    driver: "WebDriver",
    checkbox_ticked: Callable[[], bool],
    timeout: float = 28.0,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            driver.switch_to.default_content()
            url = driver.current_url or ""
            if "challenge/recaptcha" not in url:
                return True
            if checkbox_ticked():
                return True
        except Exception:
            pass
        time.sleep(0.45)
    return False


def run_free_captcha_pipeline(
    driver: "WebDriver",
    checkbox_ticked: Callable[[], bool],
    max_cycles: int = 3,
) -> bool:
    """Try audio → vision → audio retries. Returns True if captcha cleared."""
    if not free_audio_enabled() and not free_vision_enabled():
        return False

    for cycle in range(1, max_cycles + 1):
        _log(f"cycle {cycle}/{max_cycles}")
        solved = False

        if free_audio_enabled():
            _log("try audio (Whisper)")
            if try_solve_recaptcha_audio_page(driver):
                if _wait_recaptcha_cleared(driver, checkbox_ticked, timeout=22):
                    _log("cleared after audio")
                    return True
                solved = True

        if free_vision_enabled():
            _log("try image (YOLO)")
            if try_solve_recaptcha_image_page(driver, rounds=2):
                if _wait_recaptcha_cleared(driver, checkbox_ticked, timeout=22):
                    _log("cleared after vision")
                    return True
                solved = True

        if not solved:
            break
        time.sleep(1.0)

    return _wait_recaptcha_cleared(driver, checkbox_ticked, timeout=3)
