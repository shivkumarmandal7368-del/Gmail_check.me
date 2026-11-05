"""Free reCAPTCHA v2 path: accessibility audio + local Whisper (no 2Captcha)."""

from __future__ import annotations

import os
import re
import tempfile
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from selenium.webdriver.remote.webdriver import WebDriver


def _log(msg: str) -> None:
    print(f"[UC] [captcha-audio] {msg}", file=__import__("sys").stderr, flush=True)


def free_audio_enabled() -> bool:
    """On by default; set CAPTCHA_FREE_AUDIO=0 to disable."""
    return os.environ.get("CAPTCHA_FREE_AUDIO", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


_whisper_model = None


def _transcribe_audio(path: str) -> str | None:
    global _whisper_model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        _log("install faster-whisper on VPS: .venv/bin/pip install faster-whisper")
        return None
    model_name = os.environ.get("WHISPER_MODEL", "base.en")
    if _whisper_model is None:
        _log(f"loading Whisper model {model_name} (first run downloads model weights)…")
        _whisper_model = WhisperModel(model_name, device="cpu", compute_type="int8")
    best: str | None = None
    for beam in (5, 3, 1):
        segments, _ = _whisper_model.transcribe(
            path,
            language="en",
            beam_size=beam,
            best_of=3,
            temperature=0.0,
            initial_prompt="The numbers are spoken clearly for a captcha.",
            vad_filter=True,
        )
        text = " ".join(s.text for s in segments).strip()
        if not text:
            continue
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 4:
            best = digits
            _log(f"Whisper (beam={beam}) digits: {digits[:12]}")
            break
        cleaned = re.sub(r"[^a-zA-Z0-9\s]", "", text)
        cleaned = " ".join(cleaned.split())
        best = cleaned or text
        _log(f"Whisper (beam={beam}) heard: {cleaned[:80]}")
        break
    return best


def _switch_challenge_frame(driver: "WebDriver") -> bool:
    from selenium.webdriver.common.by import By

    driver.switch_to.default_content()
    frames: list = []
    try:
        frames = driver.execute_script("""
            const out = [];
            function walk(root) {
              if (!root || !root.querySelectorAll) return;
              root.querySelectorAll('iframe').forEach(f => out.push(f));
              root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
            }
            walk(document);
            return out;
        """) or []
    except Exception:
        frames = driver.find_elements(By.CSS_SELECTOR, "iframe")
    parents = list(frames)
    for parent in parents:
        try:
            driver.switch_to.default_content()
            driver.switch_to.frame(parent)
            frames.extend(driver.find_elements(By.CSS_SELECTOR, "iframe"))
        except Exception:
            pass
    driver.switch_to.default_content()
    best = None
    best_area = 0
    for fr in frames:
        try:
            src = (fr.get_attribute("src") or "").lower()
            title = (fr.get_attribute("title") or "").lower()
            if "bframe" not in src and "challenge" not in title:
                continue
            size = fr.size or {}
            width = float(size.get("width") or 0)
            height = float(size.get("height") or 0)
            # A hidden bframe can be 400x0. Both sides must be on screen.
            if width < 80 or height < 80:
                continue
            area = width * height
            if area > best_area:
                best_area = area
                best = fr
        except Exception:
            continue
    if best is None:
        return False
    try:
        driver.switch_to.default_content()
        driver.switch_to.frame(best)
        return True
    except Exception:
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
        return False


def _cdp_attrs(node: dict) -> dict:
    raw = node.get("attributes") or []
    return {raw[i]: raw[i + 1] for i in range(0, len(raw) - 1, 2)}


def _cdp_bframe(driver: "WebDriver") -> dict:
    """Challenge iframe plus the nodes inside it, including closed shadow roots."""
    try:
        doc = driver.execute_cdp_cmd("DOM.getDocument", {"depth": -1, "pierce": True})
    except Exception as exc:
        _log(f"DOM pierce failed: {exc}")
        return {}
    bframe = None
    nodes: list[dict] = []

    def walk(node, in_bf: bool = False):
        nonlocal bframe
        if not isinstance(node, dict):
            return
        attr = _cdp_attrs(node)
        name = (node.get("nodeName") or "").lower()
        src = (attr.get("src") or "")
        child_bf = in_bf
        if name == "iframe" and "bframe" in src.lower():
            bframe = {
                "backendNodeId": node.get("backendNodeId"),
                "src": src[:140],
            }
            child_bf = True
        if child_bf and node.get("backendNodeId"):
            nodes.append({
                "backendNodeId": node.get("backendNodeId"),
                "name": name,
                "id": attr.get("id") or "",
                "class": attr.get("class") or "",
                "src": src[:180],
            })
        content = node.get("contentDocument")
        if content:
            if child_bf and not bframe.get("docId"):
                bframe["docId"] = content.get("backendNodeId")
            walk(content, child_bf)
        for child in node.get("children") or []:
            walk(child, in_bf)
        for shadow in node.get("shadowRoots") or []:
            walk(shadow, in_bf)

    walk(doc.get("root") or {})
    if not bframe:
        return {}
    bframe["nodes"] = nodes
    return bframe


def _css_point(driver: "WebDriver", iframe_id, node_id):
    def quad(bid):
        if not bid:
            return None
        try:
            res = driver.execute_cdp_cmd("DOM.getContentQuads", {"backendNodeId": int(bid)})
            quads = res.get("quads") or []
            return quads[0] if quads else None
        except Exception:
            return None

    def bounds(q):
        xs, ys = q[0::2], q[1::2]
        return min(xs), min(ys), max(xs), max(ys)

    try:
        vw, vh, dpr = driver.execute_script(
            "return [window.innerWidth||412, window.innerHeight||868, window.devicePixelRatio||1]"
        )
        vw, vh, dpr = float(vw), float(vh), float(dpr or 1)
    except Exception:
        vw, vh, dpr = 412.0, 868.0, 1.0

    iq, cq = quad(iframe_id), quad(node_id)
    if cq is None:
        return None

    def fits(rect) -> bool:
        left, top, right, bottom = rect
        return right - left > 8 and bottom <= vh + 140 and right <= vw + 80 and left >= -40 and top >= -40

    raw_i = bounds(iq) if iq else None
    iframe = None
    if raw_i and fits(raw_i):
        iframe = raw_i
    elif raw_i and dpr > 1.15 and fits(tuple(v / dpr for v in raw_i)):
        iframe = tuple(v / dpr for v in raw_i)
    cl, ct, cr, cb = bounds(cq)
    cx, cy = (cl + cr) / 2.0, (ct + cb) / 2.0
    options = [(cx, cy), (cx / dpr, cy / dpr) if dpr > 1.15 else (cx, cy)]
    if iframe:
        options.append((iframe[0] + cx, iframe[1] + cy))
        options.append((iframe[0] + cx / dpr, iframe[1] + cy / dpr))
    for x, y in options:
        if iframe and iframe[0] - 8 <= x <= iframe[2] + 8 and iframe[1] - 8 <= y <= iframe[3] + 8:
            return x, y
        if iframe is None and 0 <= x <= vw and 0 <= y <= vh:
            return x, y
    return None


def _cdp_click_point(driver: "WebDriver", x: float, y: float) -> None:
    x, y = float(x), float(y)
    for typ, buttons in (("mouseMoved", 0), ("mousePressed", 1)):
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": typ, "x": x, "y": y, "button": "left", "buttons": buttons, "clickCount": 1,
        })
    time.sleep(0.07)
    driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y, "button": "left", "buttons": 0, "clickCount": 1,
    })


def _pick_node(nodes: list[dict], ids: tuple[str, ...], classes: tuple[str, ...] = ()) -> dict | None:
    for node in nodes:
        if node.get("id") in ids:
            return node
    for node in nodes:
        blob = node.get("class") or ""
        if any(c in blob for c in classes):
            return node
    return None


def _audio_bytes_from_frame(driver: "WebDriver", doc_id) -> tuple[bytes, str] | None:
    if not doc_id:
        return None
    try:
        resolved = driver.execute_cdp_cmd("DOM.resolveNode", {"backendNodeId": int(doc_id)})
        oid = (resolved.get("object") or {}).get("objectId")
        if not oid:
            return None
        res = driver.execute_cdp_cmd("Runtime.callFunctionOn", {
            "objectId": oid,
            "awaitPromise": True,
            "returnByValue": True,
            "functionDeclaration": """async function() {
                const el = document.querySelector('#audio-source, audio source, audio');
                const src = el && (el.src || el.getAttribute('src'));
                if (!src) return null;
                const r = await fetch(src);
                const buf = await r.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let i = 0; i < bytes.length; i += 0x8000) {
                  bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
                }
                return {b64: btoa(bin), type: (r.headers.get('content-type') || '')};
            }""",
        })
        value = (res.get("result") or {}).get("value") or {}
        import base64
        raw = value.get("b64")
        if not raw:
            return None
        return base64.b64decode(raw), value.get("type") or ""
    except Exception as exc:
        _log(f"in-frame audio fetch failed: {exc}")
        return None


def _try_audio_via_cdp(driver: "WebDriver") -> bool:
    """Click the audio challenge inside a shadow/cross-origin bframe."""
    frame = _cdp_bframe(driver)
    if not frame:
        _log("no bframe in DOM pierce")
        return False
    btn = _pick_node(
        frame.get("nodes") or [],
        ("recaptcha-audio-button",),
        ("rc-button-audio",),
    )
    if not btn:
        _log("audio button not in challenge frame")
        return False
    point = _css_point(driver, frame.get("backendNodeId"), btn.get("backendNodeId"))
    if not point:
        _log("audio button has no click point")
        return False
    _log(f"CDP audio button click at {int(point[0])},{int(point[1])}")
    _cdp_click_point(driver, point[0], point[1])
    payload = None
    clip_deadline = time.time() + 8.0
    while time.time() < clip_deadline and not payload:
        time.sleep(0.4)
        frame = _cdp_bframe(driver) or {}
        payload = _audio_bytes_from_frame(driver, frame.get("docId"))
    if not payload:
        _log("could not read audio from challenge frame")
        return False
    data, ctype = payload
    suffix = ".mp3" if "mpeg" in ctype or "mp3" in ctype else ".wav"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    try:
        answer = _transcribe_audio(tmp.name)
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
    if not answer:
        return False
    frame = _cdp_bframe(driver)
    field = _pick_node(
        frame.get("nodes") or [],
        ("audio-response",),
        ("rc-audiochallenge-response-field",),
    )
    if not field:
        _log("audio response field not found")
        return False
    field_point = _css_point(driver, frame.get("backendNodeId"), field.get("backendNodeId"))
    if not field_point:
        return False
    _cdp_click_point(driver, field_point[0], field_point[1])
    time.sleep(0.15)
    try:
        driver.execute_cdp_cmd("Input.insertText", {"text": answer})
    except Exception as exc:
        _log(f"insertText failed: {exc}")
        return False
    verify = _pick_node(
        frame.get("nodes") or [],
        ("recaptcha-verify-button",),
        ("rc-button-default",),
    )
    if verify:
        vpoint = _css_point(driver, frame.get("backendNodeId"), verify.get("backendNodeId"))
        if vpoint:
            time.sleep(0.2)
            _cdp_click_point(driver, vpoint[0], vpoint[1])
    _log("audio answer submitted via CDP")
    time.sleep(2.2)
    return True


def _download_challenge_audio(driver: "WebDriver", session) -> str | None:
    from selenium.webdriver.common.by import By

    url = None
    for sel in ("#audio-source", "audio source", "audio"):
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            url = el.get_attribute("src")
            if url:
                break
        except Exception:
            continue
    if not url:
        try:
            link = driver.find_element(By.CSS_SELECTOR, ".rc-audiochallenge-tdownload-link")
            url = link.get_attribute("href")
        except Exception:
            pass
    if not url:
        return None
    try:
        driver.set_script_timeout(20)
        b64 = driver.execute_async_script(
            """
            const src = arguments[0];
            const done = arguments[arguments.length - 1];
            fetch(src).then(r => r.arrayBuffer()).then(buf => {
              const bytes = new Uint8Array(buf);
              let bin = '';
              for (let i = 0; i < bytes.length; i += 0x8000) {
                bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
              }
              done(btoa(bin));
            }).catch(() => done(null));
            """,
            url,
        )
        if b64:
            import base64
            raw = base64.b64decode(b64)
            if len(raw) > 100:
                suffix = ".mp3" if url.lower().split("?")[0].endswith(".mp3") else ".wav"
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                tmp.write(raw)
                tmp.close()
                return tmp.name
    except Exception as exc:
        _log(f"in-page audio fetch failed: {exc}")
    for c in driver.get_cookies():
        session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
    r = session.get(url, timeout=30)
    r.raise_for_status()
    suffix = ".mp3" if "mpeg" in (r.headers.get("content-type") or "") else ".wav"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(r.content)
    tmp.close()
    return tmp.name


def try_solve_recaptcha_audio_page(driver: "WebDriver", attempts: int = 2) -> bool:
    """Switch to audio challenge, transcribe locally, submit. Returns True if submitted."""
    if not free_audio_enabled():
        return False
    from selenium.webdriver.common.by import By
    import requests as req

    for attempt in range(1, attempts + 1):
        try:
            if "challenge/recaptcha" not in (driver.current_url or ""):
                _log("left recaptcha page")
                return True
        except Exception:
            pass
        if not _switch_challenge_frame(driver):
            _log("no selenium challenge iframe — using CDP")
            return _try_audio_via_cdp(driver)

        audio_btn = None
        for sel in (
            "#recaptcha-audio-button",
            ".rc-button-audio",
            "button#recaptcha-audio-button",
            "button.rc-button-audio",
        ):
            try:
                audio_btn = driver.find_element(By.CSS_SELECTOR, sel)
                if audio_btn.is_displayed():
                    break
            except Exception:
                audio_btn = None
        if not audio_btn:
            _log("audio button not in selenium frame — trying CDP")
            driver.switch_to.default_content()
            return _try_audio_via_cdp(driver)
        try:
            shown = audio_btn.is_displayed()
        except Exception:
            shown = False
        if not shown:
            _log("audio button present but not displayed — clicking it anyway")

        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", audio_btn)
            audio_btn.click()
        except Exception as exc:
            _log(f"audio button click failed: {exc} — trying CDP")
            driver.switch_to.default_content()
            return _try_audio_via_cdp(driver)

        # The phone proxy often takes several seconds to paint the audio clip.
        clip_deadline = time.time() + 8.0
        session = req.Session()
        audio_path = None
        try:
            while time.time() < clip_deadline and not audio_path:
                try:
                    driver.switch_to.default_content()
                    if "challenge/recaptcha" not in (driver.current_url or ""):
                        _log("left recaptcha page")
                        return True
                except Exception:
                    pass
                if not _switch_challenge_frame(driver):
                    time.sleep(0.4)
                    continue
                try:
                    audio_path = _download_challenge_audio(driver, session)
                except Exception as exc:
                    _log(f"audio download error: {exc}")
                    audio_path = None
                if not audio_path:
                    time.sleep(0.4)
            if not audio_path:
                _log("could not download audio clip — trying the challenge frame")
                driver.switch_to.default_content()
                return _try_audio_via_cdp(driver)
            answer = _transcribe_audio(audio_path)
            if not answer:
                driver.switch_to.default_content()
                continue
            resp = None
            for sel in ("#audio-response", "input#audio-response", ".rc-audiochallenge-input-field"):
                try:
                    resp = driver.find_element(By.CSS_SELECTOR, sel)
                    break
                except Exception:
                    continue
            if not resp:
                _log("audio response input not found")
                driver.switch_to.default_content()
                return False
            resp.clear()
            resp.send_keys(answer)
            time.sleep(0.4)
            verify = None
            for sel in (
                "#recaptcha-verify-button",
                "button#recaptcha-verify-button",
                "#recaptcha-audio-verify-button",
            ):
                try:
                    verify = driver.find_element(By.CSS_SELECTOR, sel)
                    break
                except Exception:
                    continue
            if verify:
                verify.click()
            else:
                resp.send_keys("\n")
            _log(f"audio answer submitted (attempt {attempt})")
            time.sleep(2.5)
            driver.switch_to.default_content()
            return True
        finally:
            driver.switch_to.default_content()
            if audio_path:
                try:
                    os.unlink(audio_path)
                except Exception:
                    pass
    return False
