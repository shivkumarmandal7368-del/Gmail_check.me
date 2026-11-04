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
    (r"\bmotorcycle(s)?\b|\bmotorbike(s)?\b", ("motorcycle",)),
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
    if not model or not targets or not png or len(png) < 80:
        return False
    try:
        import numpy as np
        from PIL import Image

        image = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
        results = model.predict(source=image, verbose=False, conf=conf_min, imgsz=320)
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


def _grid_hits(png: bytes, targets: tuple[str, ...], tile_count: int, conf_min: float) -> set[int]:
    """Map one YOLO pass over the whole grid onto tile indexes. A motorcycle split across cells hits each cell it covers."""
    model = _load_yolo()
    if not model or not targets or not png or tile_count < 4:
        return set()
    side = int(tile_count ** 0.5)
    if side * side != tile_count:
        return set()
    try:
        import numpy as np
        from PIL import Image

        image = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
        results = model.predict(source=image, verbose=False, conf=conf_min, imgsz=640)
    except Exception as exc:
        _log(f"YOLO grid predict failed: {exc}")
        return set()
    if not results:
        return set()
    height, width = image.shape[:2]
    if width < 20 or height < 20:
        return set()
    cell_w, cell_h = width / side, height / side
    names = results[0].names or {}
    hits: set[int] = set()
    for box in results[0].boxes or []:
        cls_id = int(box.cls.item()) if hasattr(box.cls, "item") else int(box.cls)
        if names.get(cls_id, "") not in targets:
            continue
        xyxy = box.xyxy[0].tolist() if hasattr(box.xyxy[0], "tolist") else list(box.xyxy[0])
        x1, y1, x2, y2 = (float(v) for v in xyxy)
        for row in range(side):
            for col in range(side):
                cx1, cy1 = col * cell_w, row * cell_h
                cx2, cy2 = cx1 + cell_w, cy1 + cell_h
                ix1, iy1 = max(x1, cx1), max(y1, cy1)
                ix2, iy2 = min(x2, cx2), min(y2, cy2)
                if ix2 <= ix1 or iy2 <= iy1:
                    continue
                if (ix2 - ix1) * (iy2 - iy1) >= 0.12 * cell_w * cell_h:
                    hits.add(row * side + col)
    return hits


def try_solve_recaptcha_image_page(driver: "WebDriver", rounds: int = 4) -> bool:
    """Click matching tiles + verify. Returns True if submit clicked."""
    if not free_vision_enabled():
        return False
    from selenium.webdriver.common.by import By

    def _on_recaptcha() -> bool:
        try:
            driver.switch_to.default_content()
            return "challenge/recaptcha" in (driver.current_url or "")
        except Exception:
            return True

    ready_deadline = time.time() + 10
    instruction = ""
    tiles = []
    while time.time() < ready_deadline:
        if not _on_recaptcha():
            _log("left recaptcha page")
            return True
        if not _switch_challenge_frame(driver):
            time.sleep(0.4)
            continue
        try:
            if driver.find_elements(By.CSS_SELECTOR, "#audio-source, .rc-audiochallenge-control-panel"):
                driver.switch_to.default_content()
                return False
        except Exception:
            pass
        instruction = ""
        for sel in (
            ".rc-imageselect-desc-wrapper",
            ".rc-imageselect-instructions",
            "#rc-imageselect",
        ):
            try:
                instruction = driver.find_element(By.CSS_SELECTOR, sel).text or ""
                if instruction.strip():
                    break
            except Exception:
                continue
        tiles = driver.find_elements(By.CSS_SELECTOR, "td.rc-imageselect-tile")
        if not tiles:
            tiles = driver.find_elements(By.CSS_SELECTOR, ".rc-imageselect-tile")
        if instruction.strip() and tiles:
            break
        time.sleep(0.4)

    targets = _target_classes(instruction)
    if not targets or not tiles:
        _log(f"unknown instruction {instruction[:60]!r} tiles={len(tiles)}")
        driver.switch_to.default_content()
        return False
    _log(f"instruction {instruction[:50]!r} → {targets} tiles={len(tiles)}")

    conf = float(os.environ.get("YOLO_CONF", "0.18"))
    ever_clicked = False
    for rnd in range(1, rounds + 1):
        if not _on_recaptcha():
            _log("left recaptcha page")
            return True
        if not _switch_challenge_frame(driver):
            return False
        tiles = driver.find_elements(By.CSS_SELECTOR, "td.rc-imageselect-tile")
        if not tiles:
            tiles = driver.find_elements(By.CSS_SELECTOR, ".rc-imageselect-tile")
        if not tiles:
            _log(f"round {rnd}: no image tiles")
            driver.switch_to.default_content()
            return False

        hits: set[int] = set()
        table = None
        for sel in ("table.rc-imageselect-table", "#rc-imageselect-target table", "table"):
            try:
                table = driver.find_element(By.CSS_SELECTOR, sel)
                break
            except Exception:
                continue
        if table is not None:
            try:
                hits = _grid_hits(table.screenshot_as_png, targets, len(tiles), conf)
            except Exception as exc:
                _log(f"round {rnd}: grid shot failed: {exc}")
        if not hits:
            for idx, tile in enumerate(tiles):
                try:
                    if not tile.is_displayed():
                        continue
                    if _tile_has_target(tile.screenshot_as_png, targets, conf):
                        hits.add(idx)
                except Exception:
                    continue

        clicked = 0
        for idx in sorted(hits):
            if idx >= len(tiles):
                continue
            tile = tiles[idx]
            try:
                classes = (tile.get_attribute("class") or "").lower()
                if "selected" in classes:
                    continue
                tile.click()
                clicked += 1
                time.sleep(0.12)
            except Exception:
                continue
        ever_clicked = ever_clicked or clicked > 0
        _log(f"round {rnd}: clicked {clicked}/{len(tiles)} tiles")

        verify = None
        for sel in ("#recaptcha-verify-button", "button#recaptcha-verify-button"):
            try:
                verify = driver.find_element(By.CSS_SELECTOR, sel)
                break
            except Exception:
                continue
        if clicked == 0:
            # Nothing new matches. Verify only after a tile was already chosen.
            selected = False
            for tile in tiles:
                try:
                    if "selected" in (tile.get_attribute("class") or "").lower():
                        selected = True
                        break
                except Exception:
                    continue
            if not (selected or ever_clicked) or verify is None:
                driver.switch_to.default_content()
                return False
        if verify is not None and (clicked == 0 or rnd == rounds):
            verify.click()
            _log(f"round {rnd}: verify clicked")
            driver.switch_to.default_content()
            time.sleep(2.2)
            if not _on_recaptcha():
                return True
            continue
        # Dynamic grid: selected cells get a new photo. Look again before verify.
        driver.switch_to.default_content()
        time.sleep(1.3)
    return False
