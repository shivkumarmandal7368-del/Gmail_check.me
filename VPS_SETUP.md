# Vanguard MX — VPS Setup (Ubuntu / Debian)

Yeh guide tumhare **apne VPS** par project chalane ke liye hai. Replit par jo auto-setup hota tha (Nix + workflows), VPS par yeh scripts replace karti hain.

## Server requirements

| Item | Minimum |
|------|---------|
| OS | Ubuntu 22.04 / 24.04 or Debian 12 |
| RAM | **4 GB+** (Chrome automation heavy hai; 2 GB risky) |
| CPU | 2 vCPU |
| Disk | 10 GB free (Chrome cache ~200 MB) |

## Quick install (recommended)

VPS par root se:

```bash
# 1) Repo clone karo (apna GitHub URL lagao)
adduser vanguard   # optional — script bhi bana sakta hai
su - vanguard
git clone https://github.com/shivkumarmandal7368-del/Gmail_check.me.git vanguard-mx
cd vanguard-mx
git checkout cursor/vps-setup-0ab9   # jab tak PR merge na ho

# 2) Install script (root wapas)
exit
sudo bash /home/vanguard/vanguard-mx/scripts/vps-install.sh
```

Agar repo path alag hai:

```bash
sudo VANGUARD_USER=vanguard VANGUARD_REPO_DIR=/opt/vanguard-mx bash scripts/vps-install.sh
```

## Secrets (zaroori)

```bash
sudo nano /home/vanguard/vanguard-mx/deploy/.env
```

| Variable | Kya daalna hai |
|----------|----------------|
| `PROXY_URL` | `http://user:pass@rp.scrapegw.com:6060` (http scheme use karo) |
| `SESSION_SECRET` | `openssl rand -hex 32` ka output |

Save karke services start karo:

```bash
sudo systemctl start vanguard-api vanguard-web
sudo systemctl status vanguard-api vanguard-web
```

Browser mein: `http://YOUR_VPS_IP:8888/` (port **8888** taaki pehle se `:80` par jo site ho woh na chhue)

## Manual setup (step-by-step)

Agar install script use nahi karna:

```bash
# System deps
sudo apt update
sudo apt install -y curl git unzip xvfb nginx python3 python3-pip \
  fonts-liberation libgbm1 libnss3 libgtk-3-0 libxss1 libasound2

# Node 20 + pnpm
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g pnpm@10

cd vanguard-mx
pnpm install --frozen-lockfile
pip3 install --user -r artifacts/api-server/requirements.txt

# Chrome for Testing (149) — pehli baar ~180 MB download
eval "$(bash scripts/setup-chrome-for-testing.sh)"
echo "$CHROME_BINARY" && "$CHROME_BINARY" --version

# Build
pnpm --filter @workspace/api-server run build
pnpm --filter @workspace/gmail-checker run build

cp deploy/env.example deploy/.env
# edit deploy/.env

chmod +x scripts/vps-api.sh scripts/vps-web.sh
# systemd: deploy/systemd/*.service paths __APP_USER__ / __REPO_DIR__ replace karke /etc/systemd/system/ mein copy
```

## Services

| Service | Port | Role |
|---------|------|------|
| `vanguard-api` | 8080 | Express + Python Chrome checker |
| `vanguard-web` | 5173 | Built React UI (Vite preview) |
| nginx | 80 | Public entry, SSE-friendly `/api` proxy |

Logs:

```bash
journalctl -u vanguard-api -f
journalctl -u vanguard-web -f
```

Restart after code update (sirf Vanguard — nginx / doosre projects touch nahi):

```bash
cd /home/vanguard/vanguard-mx
sudo bash scripts/vps-update.sh
```

Manual equivalent:

```bash
cd /home/vanguard/vanguard-mx
git pull origin main
pnpm install --frozen-lockfile
pnpm --filter @workspace/api-server run build
pnpm --filter @workspace/gmail-checker run build
sudo systemctl restart vanguard-api vanguard-web
```

## HTTPS (optional)

DNS A record → VPS IP, phir:

```bash
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d checker.example.com
```

`deploy/nginx/vanguard.conf` mein `server_name` update karo.

## Troubleshooting

### Chrome / ChromeDriver

- Pehla job slow hoga (UC chromedriver download + cdc_ patch — code auto karta hai).
- Verify: `eval "$(bash scripts/setup-chrome-for-testing.sh)" && "$CHROME_BINARY" --version`
- Agar `libgbm` / shared library error: `sudo apt install -y libgbm1 libnss3 libgtk-3-0`

### Nix error on VPS

Purane repo mein `setup-chrome-for-testing.sh` sirf Nix use karta tha. Latest script **Nix ke bina bhi** chalti hai (`VPS mode` log line).

### Proxy / geo

- `PROXY_URL` **http://** scheme se start honi chahiye (HTTPS proxy host SSL error deta tha — Session 69 fix).
- Device check bina `PROXY_URL` ke fail hoga.

### Firewall

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
# 8080 / 5173 publicly mat kholo — sirf nginx use karo
```

### signin/rejected

Yeh VPS setup issue nahi — Google automation / account rate limit. `HANDOFF.md` Session 69 dekho.

## Files added for VPS

| Path | Purpose |
|------|---------|
| `scripts/vps-install.sh` | One-shot apt + build + systemd |
| `scripts/vps-api.sh` | API start (Chrome env + Node) |
| `scripts/vps-web.sh` | Web preview start |
| `scripts/setup-chrome-for-testing.sh` | Nix fallback for VPS |
| `deploy/env.example` | Environment template |
| `deploy/systemd/*.service` | systemd units |
| `deploy/nginx/vanguard.conf` | Reverse proxy |
