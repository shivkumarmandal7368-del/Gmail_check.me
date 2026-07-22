---
name: Resumable browser jobs
description: Pause and resume behavior for long-running Gmail browser checks.
---

Browser jobs use a resumable pause state: pause aborts only pending launches, lets already-running account checks finish and persist, and resume launches only credentials without a saved result.

**Why:** Browser checks are long-running and users need to recover from an accidental stop, tab close, or server restart without duplicating completed account checks.

**How to apply:** Preserve the original job credentials and results on disk, keep `paused`/`interrupted` jobs resumable, and do not enable resume until `checkingEmails` is empty.