---
name: Generated browser script validation
description: Validation rule for browser startup JavaScript assembled from Python templates.
---

When Python builds browser-injected JavaScript with f-strings, validate a rendered script using representative fingerprint data and a JavaScript parser before relying on the injection.

**Why:** Python syntax can pass while brace escaping still produces invalid JavaScript, causing the entire startup patch to be rejected before navigation.

**How to apply:** Keep a lightweight render-and-parse check in the verification flow whenever the generated browser template changes; test the actual output rather than only the Python template.