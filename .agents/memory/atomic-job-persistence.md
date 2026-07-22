---
name: Atomic job persistence
description: File-backed browser job snapshots must survive large writes and restarts.
---

Long-running job snapshots must be written through a per-job serialized queue using a temporary file followed by atomic rename. Invalid JSON discovered at startup should be quarantined, not silently deleted.

**Why:** Large browser job files previously overlapped during concurrent writes and could become concatenated JSON, preventing the job from being restored after a server restart.

**How to apply:** Keep the latest valid snapshot as the target file, serialize writes per job, and retain corrupt files under a recovery/quarantine name for inspection.