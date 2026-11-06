import { Router } from "express";
import { readFileSync } from "fs";
import { join } from "path";

const router = Router();

/** GET /api/build-info — git commit running on this VPS (no secrets). */
router.get("/build-info", (_req, res) => {
  const versionPath = join(process.cwd(), ".deploy-version");
  let commit = "";
  try {
    commit = readFileSync(versionPath, "utf8").trim();
  } catch {
    commit = "";
  }
  res.json({
    commit: commit || null,
    localtonetStickyFix: true,
  });
});

export default router;
