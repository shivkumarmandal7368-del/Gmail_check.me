export type BrowserResultCategory = "open" | "not_open" | "delete" | "unknown";

export type BrowserResultForCategory = {
  status: string;
  category?: BrowserResultCategory;
  reason?: string;
};

/**
 * Prefer the backend category signal. The reason fallback only exists for
 * browser results persisted before the category field was added.
 */
export function getBrowserResultCategory(result: BrowserResultForCategory): BrowserResultCategory {
  if (result.category) return result.category;
  if (result.status === "opened") return "open";
  if (result.status !== "verification_required") return "unknown";

  const reason = (result.reason ?? "").toLowerCase();
  const isDeleteReason =
    reason.includes("silently bounced back to password page (automation detected)") ||
    reason.includes("google is asking for phone/device verification") ||
    reason.includes("google requires phone or device verification (cannot bypass automatically)") ||
    reason.includes("google requires phone or device verification to continue (cannot bypass automatically)");

  return isDeleteReason ? "delete" : "not_open";
}