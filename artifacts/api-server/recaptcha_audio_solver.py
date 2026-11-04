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
    for fr in frames:
        try:
            src = (fr.get_attribute("src") or "").lower()
            title = (fr.get_attribute("title") or "").lower()
            if "bframe" not in src and "challenge" not in title:
                continue
            driver.switch_to.frame(fr)
            return True
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    return False


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
        if not _switch_challenge_frame(driver):
            _log("no visible recaptcha challenge iframe")
            return False

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
            _log("audio button not shown (Google may block audio for this session)")
            driver.switch_to.default_content()
            return False

        try:
            audio_btn.click()
        except Exception as exc:
            _log(f"audio button click failed: {exc}")
            driver.switch_to.default_content()
            return False
        time.sleep(1.4)

        session = req.Session()
        audio_path = None
        try:
            audio_path = _download_challenge_audio(driver, session)
            if not audio_path:
                _log("could not download audio clip")
                driver.switch_to.default_content()
                continue
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
