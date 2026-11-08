"""Orchestrate free captcha paths: audio (Whisper) then vision (YOLO)."""

from __future__ import annotations

import time
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver

from recaptcha_audio_solver import free_audio_enabled, try_solve_recaptcha_audio_page
from recaptcha_image_solver import free_vision_enabled, try_solve_recaptcha_image_page


def _log(msg: str) -> None:
    print(f"[UC] [captcha-free] {msg}", file=__import__("sys").stderr, flush=True)


def _wait_recaptcha_cleared(
    driver: "WebDriver",
    checkbox_ticked: Callable[[], bool],
    timeout: float = 28.0,
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            driver.switch_to.default_content()
            url = driver.execute_script("return location.href") or ""
            if "challenge/recaptcha" not in url:
                return True
            if checkbox_ticked():
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _parent_left_recaptcha(driver: "WebDriver") -> bool:
    try:
        driver.switch_to.default_content()
        href = driver.execute_script("return location.href") or ""
        return "challenge/recaptcha" not in href
    except Exception:
        return False


def _image_grid_ready(driver: "WebDriver") -> bool:
    """True when the picture challenge is already painted. Audio would only slow that down."""
    from selenium.webdriver.common.by import By
    from recaptcha_audio_solver import _switch_challenge_frame

    if not _switch_challenge_frame(driver):
        return False
    try:
        tiles = driver.find_elements(By.CSS_SELECTOR, "td.rc-imageselect-tile, .rc-imageselect-tile")
        return len(tiles) >= 4
    except Exception:
        return False
    finally:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass


def run_free_captcha_pipeline(
    driver: "WebDriver",
    checkbox_ticked: Callable[[], bool],
    max_cycles: int = 3,
    grace_s: float = 2.2,
    skip_vision: bool = False,
) -> bool:
    """Try audio → vision → audio retries. Returns True if captcha cleared."""
    if not free_audio_enabled() and not free_vision_enabled():
        return False

    # The checkbox click often reaches the password page on its own.
    # Clicking the headphone button during that hop added several seconds.
    if not _image_grid_ready(driver):
        grace = time.time() + grace_s
        while time.time() < grace:
            if _parent_left_recaptcha(driver):
                _log("left recaptcha page")
                return True
            time.sleep(0.2)

    for cycle in range(1, max_cycles + 1):
        _log(f"cycle {cycle}/{max_cycles}")
        if _parent_left_recaptcha(driver):
            _log("left recaptcha page")
            return True
        solved = False
        # Spoken digits are one clip. The picture grid takes several rounds and
        # was the slow path that still failed on cars and scooters.
        if free_audio_enabled() and not _parent_left_recaptcha(driver):
            _log("try audio first")
            if try_solve_recaptcha_audio_page(driver):
                if _wait_recaptcha_cleared(driver, checkbox_ticked, timeout=8):
                    _log("cleared after audio")
                    return True
                solved = True

        if free_vision_enabled() and not skip_vision and not _parent_left_recaptcha(driver):
            _log("try image (YOLO)")
            if try_solve_recaptcha_image_page(driver, rounds=2):
                if _wait_recaptcha_cleared(driver, checkbox_ticked, timeout=8):
                    _log("cleared after vision")
                    return True
                solved = True

        if not solved:
            break
        time.sleep(1.0)

    return _wait_recaptcha_cleared(driver, checkbox_ticked, timeout=3)
