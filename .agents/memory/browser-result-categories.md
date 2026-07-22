---
name: Browser result categories
description: Durable contract for Gmail browser-check result categorization.
---

Browser-check results should expose a stable category signal for UI bucketing: `open`, `not_open`, `delete`, or `unknown`. Human-readable reasons remain diagnostic text, not the primary API contract.

**Why:** Reason wording changes and legacy persisted results can otherwise move phone/device-verification accounts into the wrong list without a type or runtime failure.

**How to apply:** Set the category at the backend result boundary after retries, pass it through the Node/API contract, and retain a complete reason-based fallback only for older stored results.