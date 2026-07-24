---
name: Device check SSE lifecycle
description: The long-running device audit must be tied to the SSE response lifecycle, not the POST request lifecycle.
---

The device-check route must not terminate its Python child from `req.on("close")`: the request socket can close normally as soon as the POST body is consumed, while the SSE response is still streaming. Cleanup belongs on the response socket and should only kill the child when the response did not end normally.

**Why:** The first implementation stopped the audit immediately after sending the initial event, so the UI never received progress logs or screenshots. Watching the response allowed the full fingerprint.com audit to complete.

**How to apply:** Preserve this distinction whenever adding or changing long-running POST-to-SSE routes in the API server.