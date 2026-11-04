"""Free reCAPTCHA v2 image grid solver using YOLOv8n (local CPU, no API)."""

from __future__ import annotations

import io
import os
import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver

from recaptcha_audio_solver import _switch_challenge_frame


def _log(msg: str) -> None:
    print(f"[UC] [captcha-vision] {msg}", file=__import__("sys").stderr, flush=True)


def free_vision_enabled() -> bool:
    return os.environ.get("CAPTCHA_FREE_VISION", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


_yolo_model = None

# Google challenge phrases → COCO class names (YOLOv8n COCO)
_PHRASE_TO_CLASSES: list[tuple[str, tuple[str, ...]]] = [
    (r"traffic\s*light", ("traffic light",)),
    (r"\bbus(es)?\b", ("bus",)),
    (r"\bbicycle(s)?\b|\bbike(s)?\b", ("bicycle",)),
    (r"\bcar(s)?\b|\bautomobile", ("car",)),
    (r"\bmotorcycle(s)?\b|\bbike(s)?\b", ("motorcycle", "bicycle")),
    (r"fire\s*hydrant", ("fire hydrant",)),
    (r"\btruck(s)?\b", ("truck",)),
    (r"crosswalk|cross\s*walk", ("person",)),  # weak proxy
    (r"\bboat(s)?\b", ("boat",)),
    (r"stop\s*sign", ("stop sign",)),
    (r"hydrant", ("fire hydrant",)),
    (r"bridge", ("train", "truck", "car")),
    (r"stair(s)?", ("person",)),
    (r"palm\s*tree|tree", ("potted plant",)),
    (r"mountain|hill", ("person",)),
]


def _target_classes(instruction: str) -> tuple[str, ...]:
    text = (instruction or "").lower()
    for pattern, classes in _PHRASE_TO_CLASSES:
        if re.search(pattern, text):
            return classes
    return ()


def _load_yolo():
    global _yolo_model
    if _yolo_model is not None:
        return _yolo_model
    try:
        from ultralytics import YOLO
    except ImportError:
        _log("install: pip install ultralytics opencv-python-headless")
        return None
    _log("loading YOLOv8n (first run downloads ~6MB)…")
    _yolo_model = YOLO("yolov8n.pt")
    return _yolo_model


def _tile_has_target(png: bytes, targets: tuple[str, ...], conf_min: float) -> bool:
    model = _load_yolo()
    if not model or not targets:
        return False
    try:
        results = model.predict(source=png, verbose=False, conf=conf_min, imgsz=320)
    except Exception as exc:
        _log(f"YOLO predict failed: {exc}")
        return False
    if not results:
        return False
    names = results[0].names or {}
    for box in results[0].boxes or []:
        cls_id = int(box.cls.item()) if hasattr(box.cls, "item") else int(box.cls)
        label = names.get(cls_id, "")
        if label in targets:
            return True
    return False


def try_solve_recaptcha_image_page(driver: "WebDriver", rounds: int = 2) -> bool:
    """Click matching tiles + verify. Returns True if submit clicked."""
    if not free_vision_enabled():
        return False
    from selenium.webdriver.common.by import By

    for rnd in range(1, rounds + 1):
        if not _switch_challenge_frame(driver):
            return False

        # Skip if we are on audio challenge already
        try:
            if driver.find_elements(By.CSS_SELECTOR, "#audio-source, .rc-audiochallenge-control-panel"):
                driver.switch_to.default_content()
                return False
        except Exception:
            pass

        instruction = ""
        for sel in (".rc-imageselect-desc-wrapper", ".rc-imageselect-instructions", "#rc-imageselect"):
            try:
                instruction = driver.find_element(By.CSS_SELECTOR, sel).text or ""
                if instruction.strip():
                    break
            except Exception:
                continue
        targets = _target_classes(instruction)
        if not targets:
            _log(f"round {rnd}: unknown instruction {instruction[:60]!r}")
            driver.switch_to.default_content()
            return False
        _log(f"round {rnd}: {instruction[:50]!r} → {targets}")

        tiles = driver.find_elements(By.CSS_SELECTOR, "td.rc-imageselect-tile")
        if not tiles:
            tiles = driver.find_elements(By.CSS_SELECTOR, ".rc-imageselect-tile")
        if not tiles:
            _log("no image tiles found")
            driver.switch_to.default_content()
            return False

        conf = float(os.environ.get("YOLO_CONF", "0.18"))
        clicked = 0
        for tile in tiles:
            try:
                if not tile.is_displayed():
                    continue
                png = tile.screenshot_as_png
                if _tile_has_target(png, targets, conf):
                    tile.click()
                    clicked += 1
                    time.sleep(0.15)
            except Exception:
                continue
        _log(f"round {rnd}: clicked {clicked}/{len(tiles)} tiles")
        if clicked == 0:
            # fallback: click center tile only if single-image challenge
            if len(tiles) == 1:
                try:
                    tiles[0].click()
                    clicked = 1
                except Exception:
                    pass
        if clicked == 0:
            driver.switch_to.default_content()
            return False

        verify = None
        for sel in ("#recaptcha-verify-button", "button#recaptcha-verify-button"):
            try:
                verify = driver.find_element(By.CSS_SELECTOR, sel)
                break
            except Exception:
                continue
        if verify:
            verify.click()
            _log(f"round {rnd}: verify clicked")
        driver.switch_to.default_content()
        time.sleep(2.5)
        # If another image round loaded, loop
        if rnd < rounds and _switch_challenge_frame(driver):
            still_image = driver.find_elements(By.CSS_SELECTOR, "td.rc-imageselect-tile")
            driver.switch_to.default_content()
            if still_image:
                continue
        return True
    return False
