---
name: Browser result categories
description: Durable contract for Gmail browser-check result categorization.
---

Browser-check results should expose a stable category signal for UI bucketing: `open`, `not_open`, `delete`, or `unknown`. Human-readable reasons remain diagnostic text, not the primary API contract.

**Why:** Reason wording changes and legacy persisted results can otherwise move phone/device-verification accounts into the wrong list without a type or runtime failure.

**How to apply:** Set the category at the backend result boundary after retries, pass it through the Node/API contract, and retain a complete reason-based fallback only for older stored results.

The exact reason `Google requires phone or device verification (Verify your info to continue)` belongs to `not_open`, while the `cannot bypass automatically` verification variants remain `delete`.

**Why:** This distinction is intentional product behavior: the “Verify your info” result should be retained for follow-up rather than placed in the Delete bucket.

**How to apply:** Keep this exact variant out of both backend and legacy fallback Delete matchers, and cover it with a regression test.