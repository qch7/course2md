import { describe, expect, it } from "vitest";

import { backendLabel, duration, sessionLabel } from "./format";
import { parseDay, weekFor } from "./week";

describe("weekFor", () => {
  it("counts teaching weeks from the Monday of the first week", () => {
    expect(weekFor({ date: "2026-09-07" }, "2026-09-09")).toBe(1);
    expect(weekFor({ date: "2026-09-14 08:00" }, "2026-09-07")).toBe(2);
    expect(weekFor({ date: "2026-11-15" }, "2026-09-07")).toBe(10);
  });

  it("falls back to the resolver's week without a start date", () => {
    expect(weekFor({ date: "2026-09-14", week: 3 }, "")).toBe(3);
    expect(weekFor({ date: "" }, "")).toBeNull();
    expect(parseDay("not a date")).toBeNull();
  });
});

describe("format", () => {
  it("strips the course name from session titles", () => {
    expect(sessionLabel({ course: "编译原理", title: "编译原理 2026-11-16 13:30" })).toBe("2026-11-16 13:30");
    expect(sessionLabel({ course: "编译原理", title: "编译原理" })).toBe("编译原理");
  });

  it("labels backends and durations", () => {
    expect(backendLabel({ agent: "dsh", agent_model: "newapi / deepseek" })).toBe(
      "DeepSeek Harness · newapi / deepseek",
    );
    expect(duration(10, 25.5)).toBe("15.5 秒");
    expect(duration(0, 100)).toBe("");
    expect(duration(10, 200)).toBe("3 分 10 秒");
  });
});
