export type RobotCheckBucket = "not_robot" | "robot" | "unknown";

export type RobotResultLike = {
  status: string;
  reason?: string;
  robotOutcome?: string;
};

function reasonLooksLikeCaptcha(reason: string): boolean {
  const r = reason.toLowerCase();
  return (
    r.includes("image puzzle") ||
    r.includes("i'm not a robot") ||
    r.includes("not a robot") ||
    r.includes("recaptcha") ||
    r.includes("captcha")
  );
}

/** Bucket for Robot Check jobs — Not robot = password page; Robot = captcha. */
export function getRobotCheckBucket(result: RobotResultLike): RobotCheckBucket {
  if (result.status === "checking") return "unknown";
  const outcome = (result.robotOutcome ?? "").toLowerCase();
  if (outcome === "not_robot") return "not_robot";
  if (outcome === "robot") return "robot";
  if (result.status === "opened") return "not_robot";
  if (result.status === "verification_required" && reasonLooksLikeCaptcha(result.reason ?? "")) {
    return "robot";
  }
  return "unknown";
}
