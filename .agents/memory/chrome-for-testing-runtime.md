---
name: Chrome for Testing runtime
description: Why this project bootstraps Chrome separately from the Nix Chromium package.
---

For this browser automation project, use the current Chrome for Testing bundle
with Python Selenium and undetected-chromedriver. The Nix Chromium package can
lag the browser major version required by target sites, so the API workflow
must bootstrap and cache Chrome on every boot, then export its binary path,
detected major version, and shared-library path.

**Why:** The environment's packaged Chromium was version 138 while the current
stable Chrome for Testing was 151; using the old browser caused automation
compatibility failures. Chrome for Testing also needs `libgbm` and `libudev`
explicitly available in the Nix runtime closure.

**How to apply:** Keep `scripts/setup-chrome-for-testing.sh` in the API
startup path. Python launchers should read `CHROME_BINARY` and derive
`version_main` from the actual binary rather than pinning a stale major.