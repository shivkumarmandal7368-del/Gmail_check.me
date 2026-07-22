import assert from "node:assert/strict";
import { getBrowserResultCategory } from "./browserResultCategory";

const deleteReasons = [
  "Google silently bounced back to password page (automation detected). Profile wiped — auto-retrying with fresh fingerprint.",
  "Google is asking for phone/device verification",
  "Google requires phone or device verification (Verify your info to continue)",
  "Google requires phone or device verification (cannot bypass automatically)",
  "Google requires phone or device verification to continue (cannot bypass automatically)",
];

for (const reason of deleteReasons) {
  assert.equal(
    getBrowserResultCategory({ status: "verification_required", category: "delete", reason }),
    "delete",
  );
}

assert.equal(
  getBrowserResultCategory({ status: "verification_required", category: "not_open", reason: "ordinary verification" }),
  "not_open",
);
assert.equal(getBrowserResultCategory({ status: "opened", category: "open", reason: "Mailbox opened" }), "open");
assert.equal(getBrowserResultCategory({ status: "wrong_password", category: "unknown", reason: "Wrong password" }), "unknown");

// Legacy persisted results without the stable signal retain the same rules.
assert.equal(
  getBrowserResultCategory({
    status: "verification_required",
    reason: "Google requires phone or device verification (cannot bypass automatically)",
  }),
  "delete",
);
assert.equal(
  getBrowserResultCategory({ status: "verification_required", reason: "Google needs a recovery step" }),
  "not_open",
);

console.log("browser result category tests passed");