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


def _snap(driver: "WebDriver", label: str) -> None:
    fn = getattr(driver, "capture_step", None)
    if callable(fn):
        try:
            fn(label)
        except Exception:
            pass


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


def _grid_hits(
    png: bytes,
    targets: tuple[str, ...],
    tile_count: int,
    conf_min: float,
    *,
    label_floor: dict[str, float] | None = None,
    label_overlap: dict[str, float] | None = None,
    overlap: float = 0.12,
) -> set[int]:
    """Map one YOLO pass over the whole grid onto tile indexes. A motorcycle split across cells hits each cell it covers."""
    model = _load_yolo()
    if not model or not targets or not png or tile_count < 4:
        return set()
    side = int(tile_count ** 0.5)
    if side * side != tile_count:
        return set()
    floors = label_floor or {}
    overlaps = label_overlap or {}
    predict_conf = min([conf_min, *floors.values()]) if floors else conf_min
    try:
        import numpy as np
        from PIL import Image

        image = np.asarray(Image.open(io.BytesIO(png)).convert("RGB"))
        results = model.predict(source=image, verbose=False, conf=predict_conf, imgsz=640)
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
        label = names.get(cls_id, "")
        if label not in targets:
            continue
        score = float(box.conf.item()) if hasattr(box.conf, "item") else float(box.conf)
        if score < floors.get(label, conf_min):
            continue
        xyxy = box.xyxy[0].tolist() if hasattr(box.xyxy[0], "tolist") else list(box.xyxy[0])
        x1, y1, x2, y2 = (float(v) for v in xyxy)
        need = overlaps.get(label, overlap) * cell_w * cell_h
        for row in range(side):
            for col in range(side):
                cx1, cy1 = col * cell_w, row * cell_h
                cx2, cy2 = cx1 + cell_w, cy1 + cell_h
                ix1, iy1 = max(x1, cx1), max(y1, cy1)
                ix2, iy2 = min(x2, cx2), min(y2, cy2)
                if ix2 <= ix1 or iy2 <= iy1:
                    continue
                if (ix2 - ix1) * (iy2 - iy1) >= need:
                    hits.add(row * side + col)
    return hits


def _rider_fallback_targets(targets: tuple[str, ...]) -> tuple[str, ...] | None:
    """YOLOv8n labels a scooter-from-behind as person or bicycle, not motorcycle."""
    if "motorcycle" in targets:
        return ("bicycle", "person")
    return None


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
    missing_frame_since = None
    while time.time() < ready_deadline:
        if not _on_recaptcha():
            _log("left recaptcha page")
            return True
        if not _switch_challenge_frame(driver):
            # No puzzle was opened. Waiting the full 10s only burned the check.
            if missing_frame_since is None:
                missing_frame_since = time.time()
            elif time.time() - missing_frame_since >= 1.5:
                _log("no challenge frame")
                driver.switch_to.default_content()
                return False
            time.sleep(0.3)
            continue
        missing_frame_since = None
        try:
            panels = driver.find_elements(By.CSS_SELECTOR, ".rc-audiochallenge-control-panel, #audio-response")
            if any(p.is_displayed() for p in panels):
                _log("audio challenge is showing, skip image")
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
    _snap(driver, "Captcha " + " ".join(instruction.split())[:42])

    conf = float(os.environ.get("YOLO_CONF", "0.18"))
    ever_clicked = False
    rnd = 1
    max_rounds = max(rounds, 1)
    while rnd <= max_rounds:
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
        grid_png = None
        if table is not None:
            try:
                grid_png = table.screenshot_as_png
                hits = _grid_hits(grid_png, targets, len(tiles), conf)
            except Exception as exc:
                _log(f"round {rnd}: grid shot failed: {exc}")
        if not hits and grid_png is not None:
            extra = _rider_fallback_targets(targets)
            if extra:
                _log(f"round {rnd}: no {targets[0]} box, checking rider and bicycle")
                hits = _grid_hits(
                    grid_png,
                    extra,
                    len(tiles),
                    0.08,
                    label_floor={"person": 0.25, "bicycle": 0.08},
                    label_overlap={"person": 0.28, "bicycle": 0.12},
                )
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
        button_text = ""
        if verify is not None:
            try:
                button_text = (verify.text or verify.get_attribute("textContent") or "").strip().lower()
            except Exception:
                button_text = ""
        if clicked > 0 and rnd == max_rounds and max_rounds < 6:
            # New photos appear in the cells that were just selected.
            max_rounds += 1
            _log(f"round {rnd}: selected tiles, waiting for the new photos")
            driver.switch_to.default_content()
            time.sleep(2.0)
            rnd += 1
            continue
        if clicked == 0:
            # SKIP with an empty selection submits "there are none".
            # Do that only when this grid itself has no match.
            selected = False
            for tile in tiles:
                try:
                    if "selected" in (tile.get_attribute("class") or "").lower():
                        selected = True
                        break
                except Exception:
                    continue
            if button_text.startswith("skip") and not selected:
                if verify is None:
                    driver.switch_to.default_content()
                    return False
            elif not (selected or ever_clicked) or verify is None:
                driver.switch_to.default_content()
                return False
        if verify is not None and (clicked == 0 or rnd == max_rounds):
            _snap(driver, f"Captcha round {rnd} before {button_text or 'verify'}")
            verify.click()
            _log(f"round {rnd}: {button_text or 'verify'} clicked")
            driver.switch_to.default_content()
            time.sleep(2.2)
            if not _on_recaptcha():
                return True
            _log("puzzle still open after the button — switching to audio")
            driver.switch_to.default_content()
            return False
        # Dynamic grid: selected cells get a new photo. Look again before verify.
        driver.switch_to.default_content()
        time.sleep(2.0)
        rnd += 1
    return False
