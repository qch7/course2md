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
