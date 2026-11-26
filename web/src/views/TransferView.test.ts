import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createMemoryHistory, createRouter } from "vue-router";

import { client, type Course, type Job, type TransferPeer } from "@/api/client";
import { absorb, absorbCourse, resetJobs, store } from "@/stores/jobs";
import { ui } from "@/stores/ui";

import TransferView from "./TransferView.vue";

function makePeer(overrides: Partial<TransferPeer> = {}): TransferPeer {
  return {
    id: "peer-1",
    name: "实验室服务器",
    hub_url: "https://lab.example.com",
    has_token: true,
    token_hint: "c2m_...abcd",
    created_at: 1700000000,
    updated_at: 1700000000,
    ...overrides,
  };
}

function makeCourse(overrides: Partial<Course> = {}): Course {
  return {
    id: "course-1",
    title: "高等数学",
    course: "高等数学",
    source: "https://example.com/course/1",
    out_dir: "/tmp/out",
    resolver: "generic",
    resolver_label: "通用",
    sessions: [],
    created_at: 1700000000,
    updated_at: 1700000000,
    ...overrides,
  } as Course;
}

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: "job-1",
    course_id: "course-1",
    course: "高等数学",
    session_title: "第 1 讲 极限",
    session_date: "2026-10-01",
    status: "done",
    agent: "codex",
    files: [{ path: "notes/00-课程总览.md", name: "00-课程总览.md", kind: "note", size: "120 B" }],
    started_at: 1700000100,
    ...overrides,
  } as Job;
}

describe("TransferView", () => {
  let wrapper: VueWrapper;
  let queryClient: QueryClient;

  beforeEach(() => {
    resetJobs();
    ui.error = "";
    ui.toast = "";
    absorbCourse(makeCourse());
    absorb(makeJob());
    vi.spyOn(client, "GET").mockResolvedValue({
      data: { peers: [makePeer()] },
      response: new Response(),
    } as never);
    vi.spyOn(client, "POST").mockResolvedValue({
      data: {
        hub_url: "https://lab.example.com",
        courses: [],
        imported: [],
        overwritten: [],
        skipped: [],
      },
      response: new Response(),
    } as never);
    vi.spyOn(client, "DELETE").mockResolvedValue({
      data: { ok: true },
      response: new Response(),
    } as never);
  });

  afterEach(() => {
    wrapper?.unmount();
    queryClient?.clear();
    resetJobs();
    vi.restoreAllMocks();
  });

  async function render(path = "/transfer"): Promise<void> {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: "/transfer", component: TransferView }],
    });
    await router.push(path);
    await router.isReady();
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    wrapper = mount(TransferView, {
      global: { plugins: [router, [VueQueryPlugin, { queryClient }]] },
    });
    const dialog = wrapper.find("dialog").element as HTMLDialogElement;
    dialog.showModal = vi.fn(() => (dialog.open = true));
    dialog.close = vi.fn((value = "") => {
      dialog.returnValue = value;
      dialog.open = false;
      dialog.dispatchEvent(new Event("close"));
    });
    await flushPromises();
  }

  it("preselects a job from query string and pushes to the selected peer", async () => {
    vi.mocked(client.POST).mockResolvedValueOnce({
      data: {
        hub_url: "https://lab.example.com",
        courses: [makeCourse()],
        imported: [makeJob()],
        overwritten: [],
        skipped: [],
      },
      response: new Response(),
    } as never);

    await render("/transfer?jobId=job-1");
    expect(wrapper.text()).toContain("实验室服务器");
    const pushBtn = wrapper.findAll("button").find((b) => b.text().includes("推送所选（1 节）"))!;
    expect(pushBtn).toBeTruthy();
    await pushBtn.trigger("click");
    await flushPromises();

    expect(client.POST).toHaveBeenCalledWith("/api/v1/transfer/push", {
      body: {
        peer_id: "peer-1",
        hub_url: null,
        token: null,
        job_ids: ["job-1"],
        conflict: "skip",
        include_cache: false,
      },
    });
    expect(ui.toast).toContain("新增 1 节");
    expect(wrapper.find(".transfer-summary").text()).toContain("新增 1 节");
  });

  it("pushes with stage caches when the option is ticked", async () => {
    vi.mocked(client.POST).mockResolvedValueOnce({
      data: {
        hub_url: "https://lab.example.com",
        courses: [makeCourse()],
        imported: [makeJob()],
        skipped: [],
      },
      response: new Response(),
    } as never);

    await render("/transfer?jobId=job-1");
    const option = wrapper
      .findAll("label.check-line")
      .find((item) => item.text().includes("带上各阶段缓存"))!;
    await option.find("input").setValue(true);
    await wrapper
      .findAll("button")
      .find((b) => b.text().includes("推送所选（1 节）"))!
      .trigger("click");
    await flushPromises();

    expect(client.POST).toHaveBeenCalledWith(
      "/api/v1/transfer/push",
      expect.objectContaining({ body: expect.objectContaining({ include_cache: true }) }),
    );
  });

  it("fetches remote catalog and pulls selected jobs into local store", async () => {
    const pulledCourse = makeCourse({ id: "course-remote", title: "线性代数", course: "线性代数" });
    const pulledJob = makeJob({
      id: "job-remote",
      course_id: "course-remote",
      course: "线性代数",
      session_title: "第 1 讲 矩阵",
    });

    vi.mocked(client.POST)
      .mockResolvedValueOnce({
        data: {
          hub_url: "https://lab.example.com",
          mode: "server",
          courses: [
            {
              id: "course-remote",
              title: "线性代数",
              jobs: [
                {
                  id: "job-remote",
                  title: "第 1 讲 矩阵",
                  session_date: "2026-10-02",
                  status: "done",
                  file_count: 3,
                  transferable: true,
                },
              ],
            },
          ],
        },
        response: new Response(),
      } as never)
      .mockResolvedValueOnce({
        data: {
          hub_url: "https://lab.example.com",
          courses: [pulledCourse],
          imported: [pulledJob],
          overwritten: [],
          skipped: [],
        },
        response: new Response(),
      } as never);

    await render();
    const pullFromPeerBtn = wrapper.findAll("button").find((b) => b.text() === "从此 Hub 拉取")!;
    await pullFromPeerBtn.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("线性代数");
    const pullBtn = wrapper.findAll("button").find((b) => b.text().includes("拉取所选（1 节）"))!;
    await pullBtn.trigger("click");
    await flushPromises();

    expect(client.POST).toHaveBeenLastCalledWith("/api/v1/transfer/pull", {
      body: {
        peer_id: "peer-1",
        hub_url: null,
        token: null,
        job_ids: ["job-remote"],
        conflict: "skip",
        include_cache: false,
      },
    });
    expect(store.courses.get("course-remote")?.title).toBe("线性代数");
    expect(store.jobs.get("course-remote")).toBeUndefined();
    expect(store.jobs.get("job-remote")?.session_title).toBe("第 1 讲 矩阵");
  });

  it("deletes a saved peer after confirmation", async () => {
    await render();
    vi.mocked(client.GET).mockResolvedValue({ data: { peers: [] }, response: new Response() } as never);
    const deleteBtn = wrapper.findAll("button").find((b) => b.text() === "删除")!;
    await deleteBtn.trigger("click");
    expect(wrapper.find("dialog").element.open).toBe(true);
    await wrapper.find("dialog .primary-button").trigger("click");
    await flushPromises();

    expect(client.DELETE).toHaveBeenCalledWith("/api/v1/transfer/peers/{peer_id}", {
      params: { path: { peer_id: "peer-1" } },
    });
    expect(ui.toast).toBe("已移除对端 Hub");
  });

  it("imports an offline .c2m.zip bundle and absorbs results", async () => {
    const importedCourse = makeCourse({ id: "course-zip", title: "概率论", course: "概率论" });
    const importedJob = makeJob({
      id: "job-zip",
      course_id: "course-zip",
      course: "概率论",
      session_title: "第 1 讲 随机变量",
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          course: importedCourse,
          imported: [importedJob],
          overwritten: [],
          skipped: [],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await render();
    const bundleTab = wrapper.findAll('[role="tab"]').find((b) => b.text().includes(".c2m.zip"))!;
    await bundleTab.trigger("click");

    const file = new File([new Uint8Array([80, 75, 5, 6])], "prob.c2m.zip", { type: "application/zip" });
    const input = wrapper.find('input[type="file"]');
    Object.defineProperty(input.element, "files", { value: [file], configurable: true });
    await input.trigger("change");

    const importBtn = wrapper.findAll("button").find((b) => b.text() === "导入到当前 Hub")!;
    await importBtn.trigger("click");
    await flushPromises();

    expect(globalThis.fetch).toHaveBeenCalledWith(
      "/api/v1/bundles/import?conflict=skip",
      expect.objectContaining({ method: "POST" }),
    );
    expect(store.courses.get("course-zip")?.title).toBe("概率论");
    expect(store.jobs.get("job-zip")?.session_title).toBe("第 1 讲 随机变量");
  });
});
