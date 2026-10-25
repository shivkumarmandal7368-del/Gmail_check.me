import { describe, expect, it } from "vitest";
import { getRobotCheckBucket } from "./robotResultCategory";

describe("getRobotCheckBucket", () => {
  it("maps robotOutcome not_robot", () => {
    expect(getRobotCheckBucket({ status: "opened", robotOutcome: "not_robot" })).toBe("not_robot");
  });
  it("maps captcha verification to robot", () => {
    expect(getRobotCheckBucket({
      status: "verification_required",
      reason: "Google asked for an image puzzle after I'm not a robot.",
      robotOutcome: "robot",
    })).toBe("robot");
  });
});
