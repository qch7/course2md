import { afterEach, expect, it, vi } from "vitest";

afterEach(() => vi.unstubAllGlobals());

it("rejects HTTP responses from an earlier login session", async () => {
  let finish!: (response: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(() => new Promise<Response>((resolve) => (finish = resolve))),
  );
  vi.resetModules();
  const { client, resetApiSession } = await import("./client");
  const pending = client.GET("/api/v1/jobs", { baseUrl: "http://localhost" });
  await vi.waitFor(() => expect(finish).toBeDefined());
  resetApiSession();
  finish(
    new Response(JSON.stringify({ jobs: [{ id: "private" }] }), {
      headers: { "Content-Type": "application/json" },
    }),
  );
  await expect(pending).rejects.toThrow("登录状态已变更");
});

it("declares JSON on body-less writes so the origin guard accepts them", async () => {
  const seen: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      seen.push(request);
      return new Response(JSON.stringify({ ok: true }), { headers: { "Content-Type": "application/json" } });
    }),
  );
  vi.resetModules();
  const { client } = await import("./client");
  // 停止任务没有请求体；曾因缺少 Content-Type 被服务端当作跨站请求拒绝
  await client.POST("/api/v1/jobs/{job_id}/cancel", {
    baseUrl: "http://localhost",
    params: { path: { job_id: "one" } },
  });
  expect(seen[0].headers.get("content-type")).toBe("application/json");
});
