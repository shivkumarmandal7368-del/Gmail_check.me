---
name: Imported workspace dependencies
description: Dependency restoration behavior for imported pnpm monorepos in this environment.
---

For imported pnpm monorepos, the source tree and lockfile may be present while `node_modules` is absent. Restore dependencies with the existing lockfile before running package-level typechecks.

**Why:** The package-install helper is intended for adding language packages and may try to add its argument to the workspace root; it is not a replacement for a lockfile-based dependency restore.

**How to apply:** Check for `pnpm-lock.yaml` and missing `node_modules`; use a frozen pnpm install that preserves the existing manifests and lockfile, then build workspace libraries before checking dependent packages.