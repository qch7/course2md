import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { client, type Worker } from "@/api/client";
import { session } from "@/stores/session";
import { ui } from "@/stores/ui";

import WorkersView from "./WorkersView.vue";

const available = ["asr", "frames", "frame-selector:general", "hub", "llm-api"];

function worker(overrides: Partial<Worker>): Worker {
  return {
    id: "node-old",
    name: "旧电脑",
    kind: "remote",
    capabilities: [],
    disabled: true,
    online: false,
    owner_id: null,
    last_seen: 0,
    version: "",
    ...overrides,
  };
}

const hubLinksData = {
  can_manage: true,
  capabilities: available,
  default_name: "LAPTOP-TEST",
  default_work_dir: "D:\\course2md-worker",
  links: [
    {
      id: "link-1",
      hub_url: "https://course.huaiyu.me",
      name: "LAPTOP-TEST",
      status: "busy",
      worker_id: "node-9",
      capabilities: ["asr"],
      completed_tasks: 3,
      failed_tasks: 1,
      logs: ["[20:00:00] 已连接"],
    },
  ],
};

const localNodesData = {
  can_manage: true,
  available_capabilities: available,
  nodes: [
    {
      id: "builtin",
      name: "内置节点",
      builtin: true,
      enabled: true,
      auto: true,
      selected: [],
      capabilities: available,
      online: true,
      busy: false,
    },
    {
      id: "local-abc",
      name: "只做转写",
      builtin: false,
      enabled: true,
      auto: false,
      selected: ["asr"],
      capabilities: ["asr"],
      online: true,
      busy: true,
    },
  ],
};

const workersData = {
  workers: [
    worker({ id: "builtin", name: "内置节点", kind: "builtin", disabled: false, online: true }),
    worker({
      id: "node-remote",
      name: "实验室显卡机",
      disabled: false,
      online: true,
      capabilities: ["asr", "agent:dsh"],
      last_seen: 1700000000,
    }),
  ],
};

describe("mesh topology and local nodes", () => {
  let wrapper: VueWrapper;
  let queryClient: QueryClient;

  beforeEach(() => {
    session.mode = "local";
    session.user = { id: "local", username: "本机", role: "admin", disabled: false };
    ui.error = "";
    ui.toast = "";
    // jsdom 没有实现 <dialog> 的模态方法，这里补一个最小版本
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value(this: HTMLDialogElement) {
        this.open = true;
      },
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value(this: HTMLDialogElement, returnValue = "") {
        this.returnValue = returnValue;
        this.open = false;
        this.dispatchEvent(new Event("close"));
      },
    });
    vi.spyOn(client, "GET").mockImplementation((path: string) => {
      const data =
        path === "/api/v1/hub-links"
          ? hubLinksData
          : path === "/api/v1/local-nodes"
            ? localNodesData
            : workersData;
      return Promise.resolve({ data, response: new Response() }) as never;
    });
    vi.spyOn(client, "POST").mockResolvedValue({
      data: { node: { id: "local-new" } },
      response: new Response(),
    } as never);
    vi.spyOn(client, "PATCH").mockResolvedValue({
      data: { node: { id: "local-abc" } },
      response: new Response(),
    } as never);
    vi.spyOn(client, "DELETE").mockResolvedValue({ data: { ok: true }, response: new Response() });
  });

  afterEach(() => {
    wrapper?.unmount();
    queryClient?.clear();
    vi.restoreAllMocks();
    Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
    Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
  });

  async function render(): Promise<void> {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    wrapper = mount(WorkersView, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } });
    await flushPromises();
  }

  it("draws the hub in the center with colored local, remote and external vertices", async () => {
    await render();
    expect(wrapper.findAll(".mesh-vertex.kind-hub")).toHaveLength(1);
    expect(wrapper.findAll(".mesh-vertex.kind-local:not(.status-add)")).toHaveLength(2);
    expect(wrapper.findAll(".mesh-vertex.kind-remote:not(.status-add)")).toHaveLength(1);
    expect(wrapper.findAll(".mesh-vertex.kind-external:not(.status-add)")).toHaveLength(1);
    expect(wrapper.find(".mesh-vertex.kind-local.status-busy").text()).toContain("只做转写");
    expect(wrapper.find(".mesh-legend").text()).toContain("2 / 2 在线");
    // 内置节点只在「本机节点」里出现，不再混进「连入本机的节点」
    expect(wrapper.findAll(".worker-card").some((card) => card.text().includes("内置节点"))).toBe(false);
  });

  it("opens a detail dialog with grouped capabilities when a vertex is clicked", async () => {
    await render();
    await wrapper.find(".mesh-vertex.kind-remote:not(.status-add)").trigger("click");
    await flushPromises();
    const dialog = wrapper.find(".node-dialog");
    expect((dialog.element as HTMLDialogElement).open).toBe(true);
    expect(dialog.text()).toContain("实验室显卡机");
    expect(dialog.text()).toContain("连入本机的节点");
    expect(dialog.text()).toContain("转写");
    expect(dialog.text()).toContain("DSH");
    await dialog.find(".dialog-footer .quiet-button:last-child").trigger("click");
    await flushPromises();
    expect((wrapper.find(".node-dialog").element as HTMLDialogElement).open).toBe(false);
  });

  it("shows the external hub status, counters and logs in its detail dialog", async () => {
    await render();
    await wrapper.find(".mesh-vertex.kind-external:not(.status-add)").trigger("click");
    await flushPromises();
    const text = wrapper.find(".node-dialog").text();
    expect(text).toContain("course.huaiyu.me");
    expect(text).toContain("执行中");
    expect(text).toContain("3 / 1 次");
    expect(text).toContain("连接日志（1）");
  });

  it("creates a local node with a chosen subset of capabilities", async () => {
    await render();
    await wrapper.find(".mesh-vertex.kind-local.status-add").trigger("click");
    await flushPromises();
    const dialog = wrapper.find(".local-node-dialog");
    expect((dialog.element as HTMLDialogElement).open).toBe(true);
    await dialog.find("input[maxlength]").setValue("只做转写2");
    for (const box of dialog.findAll(".check-row input[type=checkbox]")) {
      const label = box.element.parentElement!.textContent!.trim();
      await box.setValue(label === "转写");
    }
    await dialog.find("form").trigger("submit");
    await flushPromises();
    expect(client.POST).toHaveBeenCalledWith("/api/v1/local-nodes", {
      body: { name: "只做转写2", auto: false, capabilities: ["asr"], enabled: true },
    });
    expect(ui.toast).toBe("本机节点已创建");
  });

  it("refuses to save a custom node without any capability", async () => {
    await render();
    await wrapper.find(".mesh-vertex.kind-local.status-add").trigger("click");
    const dialog = wrapper.find(".local-node-dialog");
    for (const box of dialog.findAll(".check-row input[type=checkbox]")) await box.setValue(false);
    expect(dialog.text()).toContain("请至少选择一项能力");
    expect(dialog.find("button[type=submit]").attributes("disabled")).toBeDefined();
    await dialog.find("form").trigger("submit");
    expect(client.POST).not.toHaveBeenCalled();
  });

  it("edits a node's capabilities from its card", async () => {
    await render();
    const card = wrapper.findAll(".local-node-card").find((item) => item.text().includes("只做转写"))!;
    await card
      .findAll("button")
      .find((button) => button.text() === "编辑")!
      .trigger("click");
    const dialog = wrapper.find(".local-node-dialog");
    expect(dialog.find("h2").text()).toBe("编辑本机节点");
    const checked = dialog
      .findAll(".check-row input[type=checkbox]")
      .filter((box) => (box.element as HTMLInputElement).checked);
    expect(checked).toHaveLength(1);
    await dialog.find("input[type=radio]").setValue(true);
    await dialog.find("form").trigger("submit");
    await flushPromises();
    expect(client.PATCH).toHaveBeenCalledWith("/api/v1/local-nodes/{node_id}", {
      params: { path: { node_id: "local-abc" } },
      body: { name: "只做转写", auto: true, capabilities: [], enabled: true },
    });
  });

  it("deletes a custom local node after confirmation but never the builtin node", async () => {
    await render();
    const builtinCard = wrapper.findAll(".local-node-card").find((item) => item.text().includes("内置节点"))!;
    expect(builtinCard.findAll("button").map((button) => button.text())).not.toContain("删除");
    const card = wrapper.findAll(".local-node-card").find((item) => item.text().includes("只做转写"))!;
    await card
      .findAll("button")
      .find((button) => button.text() === "删除")!
      .trigger("click");
    await flushPromises();
    expect(client.DELETE).not.toHaveBeenCalled();
    await wrapper.find(".confirm-dialog .primary-button").trigger("click");
    await flushPromises();
    expect(client.DELETE).toHaveBeenCalledWith("/api/v1/local-nodes/{node_id}", {
      params: { path: { node_id: "local-abc" } },
    });
    expect(ui.toast).toBe("本机节点已删除");
  });
});
