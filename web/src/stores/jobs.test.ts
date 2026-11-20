import { afterEach, describe, expect, it, vi } from "vitest";

import type { Job } from "@/api/client";
import { client } from "@/api/client";

import { isBusy, loadAll, mergeJob, resetJobs, store, watchJob } from "./jobs";

afterEach(() => {
  resetJobs();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function job(overrides: Partial<Job>): Job {
  return { id: "a", logs: [], log_offset: 0, events: [], event_offset: 0, ...overrides } as Job;
}

describe("mergeJob", () => {
  it("appends only new log lines and keeps the highest offsets", () => {
    const first = mergeJob(undefined, job({ logs: ["一", "二"], log_offset: 2 }));
    const second = mergeJob(first, job({ logs: ["二", "三"], log_offset: 3, status: "running" }));
    expect(second.logs).toEqual(["一", "二", "三"]);
    expect(second.log_offset).toBe(3);
    expect(second.status).toBe("running");
  });

  it("deduplicates events by sequence number", () => {
    const first = mergeJob(undefined, job({ events: [{ seq: 1, text: "a" } as never], event_offset: 1 }));
    const second = mergeJob(
      first,
      job({ events: [{ seq: 1, text: "a" } as never, { seq: 2, text: "b" } as never], event_offset: 2 }),
    );
    expect(second.events?.map((event) => event.seq)).toEqual([1, 2]);
  });

  it("keeps at most 500 log lines", () => {
    const logs = Array.from({ length: 600 }, (_, index) => String(index));
    expect(mergeJob(undefined, job({ logs, log_offset: 600 })).logs).toHaveLength(500);
  });
});

describe("isBusy", () => {
  it("treats queued, running and pending follow-ups as busy", () => {
    expect(isBusy(job({ status: "running" }))).toBe(true);
    expect(isBusy(job({ status: "done", pending_messages: 1 }))).toBe(true);
    expect(isBusy(job({ status: "done", pending_messages: 0 }))).toBe(false);
    expect(isBusy(undefined)).toBe(false);
  });
});

describe("account isolation", () => {
  it("replaces the previous account's jobs on reload", async () => {
    store.jobs.set("old-account", job({ id: "old-account", messages: [{ text: "private" } as never] }));
    vi.spyOn(client, "GET").mockImplementation(
      async (path) =>
        ({
          data: path === "/api/v1/jobs" ? { jobs: [job({ id: "new-account" })] } : { courses: [] },
          response: new Response(),
        }) as never,
    );
    await loadAll();
    expect([...store.jobs.keys()]).toEqual(["new-account"]);
  });

  it("ignores a pending load after logout", async () => {
    let finish!: (value: never) => void;
    const response = new Promise<never>((resolve) => (finish = resolve));
    vi.spyOn(client, "GET").mockReturnValue(response);
    const pending = loadAll();
    resetJobs();
    finish({ data: { jobs: [job({ id: "old-account" })], courses: [] }, response: new Response() } as never);
    await pending;
    expect(store.jobs.size).toBe(0);
    expect(store.loaded).toBe(false);
  });

  it("closes subscriptions and ignores queued events after logout", () => {
    const source = new EventTarget();
    const close = vi.fn();
    vi.stubGlobal(
      "EventSource",
      class {
        close = close;
        addEventListener = source.addEventListener.bind(source);
      },
    );
    store.jobs.set("a", job({ status: "running" }));
    watchJob("a");
    resetJobs();
    source.dispatchEvent(new MessageEvent("job", { data: JSON.stringify(job({})) }));
    expect(close).toHaveBeenCalledOnce();
    expect(store.jobs.size).toBe(0);
  });
});
