export type BrowserResultCategory = "open" | "not_open" | "delete" | "unknown";

export type BrowserResultForCategory = {
  status: string;
  category?: BrowserResultCategory;
  reason?: string;
};

function isDeleteReason(reason: string): boolean {
  return (
    reason.includes("silently bounced back to password page (automation detected)") ||
    reason.includes("google is asking for phone/device verification")
  );
}

/**
 * Prefer the backend category signal. The reason fallback only exists for
 * browser results persisted before the category field was added.
 */
export function getBrowserResultCategory(result: BrowserResultForCategory): BrowserResultCategory {
  if (result.status === "opened") return "open";
  if (result.status !== "verification_required") return "unknown";

  const reason = (result.reason ?? "").toLowerCase();
  // Re-check Delete results by reason so stale persisted results created by
  // the previous broader rule cannot remain in the Delete bucket.
  if (result.category === "delete") return isDeleteReason(reason) ? "delete" : "not_open";
  if (result.category) return result.category;

  return isDeleteReason(reason) ? "delete" : "not_open";
}