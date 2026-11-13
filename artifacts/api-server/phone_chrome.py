"""Attach the checker to the owner's Android Chrome over Tailscale + adb.

The VPS Chrome is a Linux browser. This module opens a tab in the phone's
own Chrome instead, so Google sees that phone and its network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "deploy" / "phone-adb.json"
DEVTOOLS_SOCKET = "localabstract:chrome_devtools_remote"


class PhoneNotReady(RuntimeError):
    pass


def _run(args: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _adb() -> str:
    path = shutil.which("adb") or "/usr/local/bin/adb"
    if not os.path.isfile(path):
        raise PhoneNotReady("VPS par adb nahi mila. Phone browser connect nahi ho sakta.")
    return path


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text())
    except Exception:
        data = {}
    if not isinstance(data, dict):
        return {}
    return data


def save_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data))
    try:
        os.chmod(STATE_PATH, 0o600)
    except OSError:
        pass


def tailscale_phone() -> dict:
    """Find the Android peer. Never includes auth keys."""
    try:
        proc = _run(["tailscale", "status", "--json"], timeout=8)
    except FileNotFoundError:
        return {"online": False, "name": "", "ip": "", "detail": "Is server par Tailscale nahi hai"}
    if proc.returncode != 0:
        return {
            "online": False,
            "name": "",
            "ip": "",
            "detail": "Tailscale status nahi mila",
        }
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"online": False, "name": "", "ip": "", "detail": "Tailscale status parse nahi hua"}
    peers = payload.get("Peer") or {}
    android = []
    for peer in peers.values():
        if not isinstance(peer, dict):
            continue
        os_name = str(peer.get("OS") or "").lower()
        if os_name != "android":
            continue
        ips = peer.get("TailscaleIPs") or []
        android.append({
            "online": bool(peer.get("Online")),
            "name": str(peer.get("HostName") or "phone"),
            "ip": str(ips[0]) if ips else "",
            "lastSeen": str(peer.get("LastSeen") or ""),
        })
    if not android:
        return {"online": False, "name": "", "ip": "", "detail": "Tailscale par koi Android phone nahi"}
    android.sort(key=lambda item: (not item["online"], item["name"]))
    chosen = android[0]
    chosen["detail"] = "online" if chosen["online"] else "offline"
    return chosen


def phone_status() -> dict:
    phone = tailscale_phone()
    saved = load_state()
    adb_state = "missing"
    try:
        adb = _adb()
        listed = _run([adb, "devices"], timeout=8)
        text = listed.stdout or ""
        if "\tdevice" in text:
            adb_state = "device"
        elif "\tunauthorized" in text:
            adb_state = "unauthorized"
        elif "\toffline" in text:
            adb_state = "offline"
        else:
            adb_state = "none"
    except PhoneNotReady as exc:
        return {
            "ready": False,
            "online": phone.get("online") is True,
            "name": phone.get("name") or "",
            "ip": phone.get("ip") or "",
            "adb": "missing",
            "paired": bool(saved.get("port")),
            "message": str(exc),
        }
    ready = phone.get("online") is True and adb_state == "device"
    if not phone.get("online"):
        message = (
            f"{phone.get('name') or 'Phone'} Tailscale par offline hai. "
            "Phone par Tailscale app khol ke Connected karo."
        )
    elif adb_state != "device":
        message = (
            "Ek hi phone aur sirf mobile data par Realme Wireless debugging on nahi karta. "
            "Mera phone off rakho. Check VPS browser se hoga, internet aapke Realme ka hi rahega."
        )
    else:
        message = f"{phone.get('name') or 'Phone'} ka Chrome ready hai."
    return {
        "ready": ready,
        "online": phone.get("online") is True,
        "name": phone.get("name") or "",
        "ip": phone.get("ip") or saved.get("host") or "",
        "adb": adb_state,
        "paired": bool(saved.get("port")),
        "connect": f"{saved.get('host')}:{saved.get('port')}" if saved.get("port") else "",
        "message": message,
    }


def pair_phone(host: str, pair_port: int, code: str, connect_port: int) -> dict:
    host = (host or "").strip()
    code = (code or "").strip()
    if not host or not code or pair_port <= 0 or connect_port <= 0:
        raise PhoneNotReady("Host, pairing port, pairing code, aur connect port chahiye.")
    adb = _adb()
    paired = _run([adb, "pair", f"{host}:{pair_port}", code], timeout=25)
    combined = f"{paired.stdout}\n{paired.stderr}"
    if paired.returncode != 0 or "failed" in combined.lower():
        detail = " ".join(combined.split())[:180]
        raise PhoneNotReady(f"Pair nahi hua. {detail}")
    connected = _run([adb, "connect", f"{host}:{connect_port}"], timeout=20)
    connect_text = f"{connected.stdout}\n{connected.stderr}"
    if connected.returncode != 0 or "connected" not in connect_text.lower():
        detail = " ".join(connect_text.split())[:180]
        raise PhoneNotReady(f"Pair ho gaya, connect nahi hua. {detail}")
    save_state({"host": host, "port": connect_port})
    return phone_status()


def _ensure_connected() -> tuple[str, str]:
    phone = tailscale_phone()
    if not phone.get("online"):
        raise PhoneNotReady(
            f"{phone.get('name') or 'Phone'} Tailscale par offline hai. "
            "Phone par Tailscale kholo, phir check dubara chalao."
        )
    saved = load_state()
    host = str(saved.get("host") or phone.get("ip") or "").strip()
    port = int(saved.get("port") or 0)
    if not host or port <= 0:
        raise PhoneNotReady(
            "Phone abhi paired nahi hai. Wireless debugging ka pairing code UI mein daalo."
        )
    adb = _adb()
    _run([adb, "connect", f"{host}:{port}"], timeout=15)
    listed = _run([adb, "devices"], timeout=8)
    if "\tunauthorized" in (listed.stdout or ""):
        raise PhoneNotReady("Phone ne debugging allow nahi kiya. Phone par Allow dabao.")
    if "\tdevice" not in (listed.stdout or ""):
        raise PhoneNotReady(
            "Wireless debugging band hai. Phone par Developer options → Wireless debugging on karo "
            "aur connect port dubara pair karo."
        )
    return adb, host


def forward_devtools() -> int:
    """Return the local TCP port that speaks Chrome DevTools on the phone."""
    adb, _host = _ensure_connected()
    _run([
        adb, "shell", "am", "start",
        "-a", "android.intent.action.VIEW",
        "-d", "about:blank",
        "-n", "com.android.chrome/com.google.android.apps.chrome.Main",
    ], timeout=15)
    listed = _run([adb, "forward", "--list"], timeout=8)
    for line in (listed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == DEVTOOLS_SOCKET and parts[1].startswith("tcp:"):
            return int(parts[1].split(":", 1)[1])
    proc = _run([adb, "forward", "tcp:0", DEVTOOLS_SOCKET], timeout=10)
    text = (proc.stdout or "").strip()
    if proc.returncode != 0 or not text.isdigit():
        raise PhoneNotReady(
            "Phone Chrome ka debug socket nahi mila. Chrome kholo aur USB debugging on rakho."
        )
    return int(text)


def main() -> int:
    import sys
    args = sys.argv[1:]
    try:
        if not args or args[0] == "status":
            print(json.dumps(phone_status()))
            return 0
        if args[0] == "pair":
            def flag(name: str) -> str:
                if name not in args:
                    return ""
                return args[args.index(name) + 1]
            result = pair_phone(
                flag("--host"),
                int(flag("--pair-port") or "0"),
                flag("--code"),
                int(flag("--connect-port") or "0"),
            )
            print(json.dumps(result))
            return 0
        print(json.dumps({"ready": False, "message": "Unknown phone command"}))
        return 1
    except PhoneNotReady as exc:
        print(json.dumps({"ready": False, "message": str(exc)}))
        return 1
    except Exception as exc:
        print(json.dumps({"ready": False, "message": str(exc)[:200]}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


def release_forward(port: int) -> None:
    try:
        adb = _adb()
    except PhoneNotReady:
        return
    if port:
        _run([adb, "forward", "--remove", f"tcp:{port}"], timeout=8)
