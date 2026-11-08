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
  if (result.status === "opened" || result.status === "password_ok" || result.status === "totp_ok") return "open";

  if (result.status === "wrong_password" || result.status === "password_changed" || result.status === "totp_wrong") return "delete";

  const reasonLower = (result.reason ?? "").toLowerCase();
  if (
    reasonLower.includes("password changed")
    || (reasonLower.includes("password") && reasonLower.includes("changed") && reasonLower.includes("ago"))
  ) return "delete";

  if (result.status === "2fa_required") return "not_open";

  // "did not say sahi, changed, or wrong" contains the word changed.
  // That sentence means the page was unclear, so it stays unknown.
  if (result.status === "unknown") return "unknown";

  if (result.status !== "verification_required") return "unknown";

  const reason = reasonLower;
  // Re-check Delete results by reason so stale persisted results created by
  // the previous broader rule cannot remain in the Delete bucket.
  if (result.category === "delete") return isDeleteReason(reason) ? "delete" : "not_open";
  if (result.category) return result.category;

  return isDeleteReason(reason) ? "delete" : "not_open";
}