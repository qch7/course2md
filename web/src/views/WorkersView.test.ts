import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { client, type Worker } from "@/api/client";
import { session } from "@/stores/session";
import { ui } from "@/stores/ui";

import WorkersView from "./WorkersView.vue";

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

describe("worker deletion", () => {
  let wrapper: VueWrapper;
  let queryClient: QueryClient;

  beforeEach(() => {
    session.mode = "local";
    session.user = { id: "local", username: "本机", role: "admin", disabled: false };
    ui.error = "";
    ui.toast = "";
    vi.spyOn(client, "GET").mockResolvedValue({
      data: {
        workers: [worker({ id: "builtin", name: "内置节点", kind: "builtin", disabled: false }), worker({})],
      },
      response: new Response(),
    } as never);
    vi.spyOn(client, "DELETE").mockResolvedValue({ data: { ok: true }, response: new Response() });
  });

  afterEach(() => {
    wrapper?.unmount();
    queryClient?.clear();
    vi.restoreAllMocks();
  });

  async function render(): Promise<void> {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    wrapper = mount(WorkersView, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } });
    const dialog = wrapper.find("dialog").element as HTMLDialogElement;
    dialog.showModal = vi.fn(() => (dialog.open = true));
    dialog.close = vi.fn((value = "") => {
      dialog.returnValue = value;
      dialog.open = false;
      dialog.dispatchEvent(new Event("close"));
    });
    await flushPromises();
  }

  function deleteButton() {
    return wrapper.findAll("button").find((button) => button.text() === "删除")!;
  }

  it("deletes a disabled node after confirmation and refreshes the list", async () => {
    await render();
    expect(wrapper.findAll(".worker-card")[0]!.findAll("button")).toHaveLength(0);
    vi.mocked(client.GET).mockResolvedValue({ data: { workers: [] }, response: new Response() } as never);
    await deleteButton().trigger("click");
    expect(wrapper.find("dialog").element.open).toBe(true);
    expect(wrapper.find("dialog").text()).toContain("旧电脑");
    expect(client.DELETE).not.toHaveBeenCalled();
    await wrapper.find("dialog .primary-button").trigger("click");
    await flushPromises();
    expect(client.DELETE).toHaveBeenCalledWith("/api/v1/workers/{worker_id}", {
      params: { path: { worker_id: "node-old" }, query: { permanent: true } },
    });
    expect(ui.toast).toBe("节点已删除");
    expect(wrapper.findAll(".worker-card")).toHaveLength(0);
  });

  it("keeps the node when confirmation is cancelled", async () => {
    await render();
    await deleteButton().trigger("click");
    await wrapper.find("dialog .quiet-button").trigger("click");
    expect(client.DELETE).not.toHaveBeenCalled();
    expect(wrapper.findAll(".worker-card")).toHaveLength(2);
  });

  it("shows the server error and keeps the node when deletion fails", async () => {
    vi.mocked(client.DELETE).mockRejectedValue(new Error("节点仍有未完成任务"));
    await render();
    await deleteButton().trigger("click");
    await wrapper.find("dialog .primary-button").trigger("click");
    await flushPromises();
    expect(ui.error).toBe("节点仍有未完成任务");
    expect(wrapper.findAll(".worker-card")).toHaveLength(2);
    expect(deleteButton().attributes("disabled")).toBeUndefined();
  });

  it("hides management actions for shared nodes and allows the owner's nodes", async () => {
    session.mode = "server";
    session.user = { id: "alice", username: "Alice", role: "user", disabled: false };
    vi.mocked(client.GET).mockResolvedValue({
      data: { workers: [worker({}), worker({ id: "node-mine", owner_id: "alice", disabled: false })] },
      response: new Response(),
    } as never);
    await render();
    const cards = wrapper.findAll(".worker-card");
    expect(cards[0]!.findAll("button")).toHaveLength(0);
    expect(cards[1]!.findAll("button").map((button) => button.text())).toEqual(["停用", "删除"]);
  });

  it("cancels with Escape or the close button without deleting the node", async () => {
    await render();
    await deleteButton().trigger("click");
    await wrapper.find("dialog").trigger("cancel");
    await flushPromises();
    expect(wrapper.find("dialog").element.open).toBe(false);
    await deleteButton().trigger("click");
    await wrapper.find("dialog .icon-button").trigger("click");
    await flushPromises();
    expect(wrapper.find("dialog").element.open).toBe(false);
    expect(client.DELETE).not.toHaveBeenCalled();
  });

  it("cancels a pending confirmation when leaving the page", async () => {
    await render();
    await deleteButton().trigger("click");
    wrapper.unmount();
    await flushPromises();
    expect(client.DELETE).not.toHaveBeenCalled();
  });

  it("uses the same dialog to confirm node disabling", async () => {
    vi.mocked(client.GET).mockResolvedValue({
      data: { workers: [worker({ disabled: false })] },
      response: new Response(),
    } as never);
    await render();
    await wrapper.find(".worker-actions button").trigger("click");
    expect(wrapper.find("dialog h2").text()).toBe("停用节点");
    expect(client.DELETE).not.toHaveBeenCalled();
    await wrapper.find("dialog .primary-button").trigger("click");
    await flushPromises();
    expect(client.DELETE).toHaveBeenCalledWith("/api/v1/workers/{worker_id}", {
      params: { path: { worker_id: "node-old" } },
    });
    expect(ui.toast).toBe("节点已停用");
  });
});
