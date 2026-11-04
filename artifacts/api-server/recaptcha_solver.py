"""Optional reCAPTCHA v2 solver (2Captcha / CapSolver) for Google sign-in puzzles."""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests


def _log(msg: str) -> None:
    print(f"[UC] [captcha] {msg}", flush=True)


def captcha_solver_configured() -> bool:
    return bool((os.environ.get("CAPTCHA_API_KEY") or os.environ.get("TWOCAPTCHA_API_KEY") or "").strip())


def _api_key() -> str:
    return (os.environ.get("CAPTCHA_API_KEY") or os.environ.get("TWOCAPTCHA_API_KEY") or "").strip()


def _provider() -> str:
    return (os.environ.get("CAPTCHA_PROVIDER") or "2captcha").strip().lower()


def _timeout_sec() -> int:
    try:
        return max(60, min(600, int(os.environ.get("CAPTCHA_TIMEOUT", "180"))))
    except ValueError:
        return 180


def _proxy_for_solver(proxy_url: str | None) -> tuple[str | None, str | None]:
    """Return (proxy_string, proxy_type) for solver APIs."""
    if not proxy_url:
        return None, None
    try:
        p = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
        host = p.hostname or ""
        port = p.port or 80
        user = p.username or ""
        password = p.password or ""
        if not host:
            return None, None
        if user:
            from urllib.parse import quote

            auth = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        else:
            auth = f"{host}:{port}"
        return auth, "HTTP"
    except Exception:
        return None, None


def extract_recaptcha_params(driver) -> dict[str, str | None]:
    """Read sitekey and optional enterprise data-s from the live page."""
    sitekey: str | None = None
    data_s: str | None = None
    try:
        found = driver.execute_script(
            """
            var sk = null, ds = null;
            var el = document.querySelector('[data-sitekey]');
            if (el) sk = el.getAttribute('data-sitekey');
            var frames = document.querySelectorAll('iframe[src*="recaptcha"]');
            for (var i = 0; i < frames.length; i++) {
              var src = frames[i].getAttribute('src') || '';
              var m = src.match(/[?&]k=([^&]+)/);
              if (m) { sk = decodeURIComponent(m[1]); break; }
            }
            if (typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg) {
              try {
                var clients = ___grecaptcha_cfg.clients || {};
                Object.keys(clients).forEach(function(cid) {
                  var c = clients[cid];
                  Object.keys(c || {}).forEach(function(k) {
                    var entry = c[k];
                    if (entry && entry.sitekey && !sk) sk = entry.sitekey;
                  });
                });
              } catch (e) {}
            }
            var sEl = document.querySelector('[data-s]');
            if (sEl) ds = sEl.getAttribute('data-s');
            return { sitekey: sk, dataS: ds };
            """
        )
        if isinstance(found, dict):
            sitekey = (found.get("sitekey") or "").strip() or None
            data_s = (found.get("dataS") or "").strip() or None
    except Exception as exc:
        _log(f"sitekey read failed: {exc}")

    if not sitekey:
        try:
            from selenium.webdriver.common.by import By

            for iframe in driver.find_elements(By.CSS_SELECTOR, "iframe[src*='recaptcha']"):
                src = iframe.get_attribute("src") or ""
                qs = parse_qs(urlparse(src).query)
                if qs.get("k"):
                    sitekey = qs["k"][0]
                    break
        except Exception:
            pass

    return {"sitekey": sitekey, "data_s": data_s}


def inject_recaptcha_token(driver, token: str) -> bool:
    try:
        driver.execute_script(
            """
            var token = arguments[0];
            document.querySelectorAll(
              'textarea[name="g-recaptcha-response"], #g-recaptcha-response'
            ).forEach(function(el) {
              el.value = token;
              el.innerHTML = token;
            });
            if (typeof ___grecaptcha_cfg !== 'undefined' && ___grecaptcha_cfg.clients) {
              try {
                Object.values(___grecaptcha_cfg.clients).forEach(function(client) {
                  Object.values(client).forEach(function(entry) {
                    if (entry && typeof entry.callback === 'function') {
                      entry.callback(token);
                    }
                  });
                });
              } catch (e) {}
            }
            return true;
            """,
            token,
        )
        return True
    except Exception as exc:
        _log(f"token inject failed: {exc}")
        return False


def _solve_2captcha(
    sitekey: str,
    page_url: str,
    proxy_url: str | None,
    data_s: str | None,
) -> str | None:
    key = _api_key()
    proxy, proxy_type = _proxy_for_solver(proxy_url)
    payload: dict[str, Any] = {
        "key": key,
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": page_url,
        "json": 1,
    }
    if data_s:
        payload["enterprise"] = 1
        payload["data-s"] = data_s
    if proxy:
        payload["proxy"] = proxy
        payload["proxytype"] = proxy_type or "HTTP"

    _log("submitting puzzle to 2Captcha…")
    try:
        r = requests.post("https://2captcha.com/in.php", data=payload, timeout=45)
        data = r.json()
    except Exception as exc:
        _log(f"2Captcha submit error: {exc}")
        return None
    if data.get("status") != 1:
        _log(f"2Captcha submit rejected: {data.get('request', data)}")
        return None
    req_id = str(data.get("request", ""))
    deadline = time.time() + _timeout_sec()
    while time.time() < deadline:
        time.sleep(5)
        try:
            res = requests.get(
                "https://2captcha.com/res.php",
                params={"key": key, "action": "get", "id": req_id, "json": 1},
                timeout=30,
            )
            body = res.json()
        except Exception as exc:
            _log(f"2Captcha poll error: {exc}")
            continue
        if body.get("status") == 1:
            token = body.get("request")
            if token:
                _log("2Captcha returned token")
                return str(token)
        if body.get("request") != "CAPCHA_NOT_READY":
            _log(f"2Captcha failed: {body.get('request', body)}")
            return None
    _log("2Captcha timed out")
    return None


def _solve_capsolver(
    sitekey: str,
    page_url: str,
    proxy_url: str | None,
    data_s: str | None,
) -> str | None:
    key = _api_key()
    task: dict[str, Any] = {
        "type": "ReCaptchaV2EnterpriseTask" if data_s else "ReCaptchaV2Task",
        "websiteURL": page_url,
        "websiteKey": sitekey,
    }
    if data_s:
        task["enterprisePayload"] = {"s": data_s}
    proxy, _ = _proxy_for_solver(proxy_url)
    if proxy:
        task["type"] = "ReCaptchaV2EnterpriseTask" if data_s else "ReCaptchaV2Task"
        task["proxy"] = f"http:{proxy}"

    _log(f"submitting puzzle to CapSolver ({task['type']})…")
    try:
        create = requests.post(
            "https://api.capsolver.com/createTask",
            json={"clientKey": key, "task": task},
            timeout=45,
        )
        created = create.json()
    except Exception as exc:
        _log(f"CapSolver create error: {exc}")
        return None
    if created.get("errorId"):
        _log(f"CapSolver create failed: {created.get('errorDescription', created)}")
        return None
    task_id = created.get("taskId")
    if not task_id:
        return None
    deadline = time.time() + _timeout_sec()
    while time.time() < deadline:
        time.sleep(3)
        try:
            res = requests.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": key, "taskId": task_id},
                timeout=30,
            )
            body = res.json()
        except Exception as exc:
            _log(f"CapSolver poll error: {exc}")
            continue
        if body.get("status") == "ready":
            token = (body.get("solution") or {}).get("gRecaptchaResponse")
            if token:
                _log("CapSolver returned token")
                return str(token)
            return None
        if body.get("errorId"):
            _log(f"CapSolver failed: {body.get('errorDescription', body)}")
            return None
    _log("CapSolver timed out")
    return None


def solve_recaptcha_v2(
    sitekey: str,
    page_url: str,
    proxy_url: str | None = None,
    data_s: str | None = None,
) -> str | None:
    if not captcha_solver_configured():
        return None
    sitekey = (sitekey or "").strip()
    if not sitekey:
        return None
    provider = _provider()
    if provider == "capsolver":
        return _solve_capsolver(sitekey, page_url, proxy_url, data_s)
    return _solve_2captcha(sitekey, page_url, proxy_url, data_s)


def try_solve_recaptcha_on_page(driver, proxy_url: str | None = None) -> bool:
    """Extract params, call solver, inject token. Returns True if token applied."""
    if not captcha_solver_configured():
        _log("CAPTCHA_API_KEY not set — skip solver")
        return False
    params = extract_recaptcha_params(driver)
    sitekey = params.get("sitekey")
    if not sitekey:
        _log("no sitekey on page — cannot solve")
        return False
    page_url = driver.current_url or "https://accounts.google.com/"
    token = solve_recaptcha_v2(
        sitekey,
        page_url,
        proxy_url=proxy_url,
        data_s=params.get("data_s"),
    )
    if not token:
        return False
    if not inject_recaptcha_token(driver, token):
        return False
    try:
        driver.execute_script(
            """
            var buttons = Array.from(document.querySelectorAll('button, div[role="button"]'));
            var next = buttons.find(function(b) {
              var t = (b.innerText || b.textContent || '').trim().toLowerCase();
              return t === 'next' || t === 'verify' || t === 'continue';
            });
            if (next) next.click();
            """
        )
    except Exception:
        pass
    time.sleep(2.5)
    return True
