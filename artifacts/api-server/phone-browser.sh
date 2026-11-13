#!/data/data/com.termux/files/usr/bin/bash
# Start a browser on this phone and let the VPS checker drive it over Tailscale.
# Wi-Fi and Wireless debugging are not used. Keep Termux open while checks run.
if [ -z "${PREFIX:-}" ]; then
  echo "Yeh command Termux ke andar paste karo. Chrome ke address bar mein nahi."
  exit 1
fi

set -e
echo "Packages install ho rahe hain. Pehli baar kuch minute lagenge."
pkg update -y
pkg install -y tur-repo || true
pkg install -y chromium socat || pkg install -y chromium-browser socat

BIN=""
if command -v chromium-browser >/dev/null 2>&1; then
  BIN="chromium-browser"
elif command -v chromium >/dev/null 2>&1; then
  BIN="chromium"
else
  echo "Chromium install nahi hua. Termux mein 'pkg install tur-repo' phir 'pkg install chromium' chalao."
  exit 1
fi

if [ -f "$PREFIX/var/run/phone-browser.pid" ]; then
  while read -r oldpid; do
    kill "$oldpid" 2>/dev/null || true
  done < "$PREFIX/var/run/phone-browser.pid"
fi
mkdir -p "$PREFIX/var/run"
pkill -f "remote-debugging-port=9223" 2>/dev/null || true
pkill -f "TCP-LISTEN:9222" 2>/dev/null || true
sleep 1

echo "Browser start ho raha hai."
"$BIN" \
  --headless=new \
  --remote-debugging-port=9223 \
  --remote-allow-origins=* \
  --no-sandbox \
  --disable-dev-shm-usage \
  --disable-gpu \
  about:blank >/data/data/com.termux/files/home/phone-browser.log 2>&1 &
echo $! >> "$PREFIX/var/run/phone-browser.pid"
sleep 2

socat TCP-LISTEN:9222,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9223 >/dev/null 2>&1 &
echo $! >> "$PREFIX/var/run/phone-browser.pid"
sleep 1

if ! curl -fsS "http://127.0.0.1:9222/json/version"; then
  echo
  echo "Browser port nahi khula. Log: ~/phone-browser.log"
  exit 1
fi
echo
echo "READY. Termux ko band mat karna. Checker par Mera phone on karke check chalao."
