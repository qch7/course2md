import { QueryClient, VueQueryPlugin } from "@tanstack/vue-query";
import { flushPromises, mount, type VueWrapper } from "@vue/test-utils";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { client, type PluginsResponse } from "@/api/client";
import { session } from "@/stores/session";
import { ui } from "@/stores/ui";

import PluginsView from "./PluginsView.vue";

function listing(overrides: Partial<PluginsResponse> = {}): PluginsResponse {
  return {
    plugins: [
      {
        name: "local",
        label: "本地视频",
        hint: "本地视频文件或目录",
        version: "",
        origin: "builtin",
        resolvers: ["local"],
        removable: false,
      },
      {
        name: "course2md-demo",
        label: "示例课堂",
        hint: "粘贴示例课堂链接",
        version: "0.1.0",
        origin: "package",
        resolvers: ["demo"],
        removable: true,
      },
    ],
    errors: [],
    can_manage: true,
    restart_required: false,
    message: "",
    ...overrides,
  };
}

describe("plugin management", () => {
  let wrapper: VueWrapper;
  let queryClient: QueryClient;

  beforeEach(() => {
    session.mode = "local";
    ui.error = "";
    ui.toast = "";
    vi.spyOn(client, "GET").mockResolvedValue({ data: listing(), response: new Response() } as never);
    vi.spyOn(client, "POST").mockResolvedValue({
      data: listing({ restart_required: true, message: "插件已安装" }),
      response: new Response(),
    } as never);
    vi.spyOn(client, "DELETE").mockResolvedValue({
      data: listing({ plugins: [listing().plugins[0]!], restart_required: true, message: "插件已删除" }),
      response: new Response(),
    } as never);
  });

  afterEach(() => {
    wrapper?.unmount();
    queryClient?.clear();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  async function render(): Promise<void> {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    wrapper = mount(PluginsView, { global: { plugins: [[VueQueryPlugin, { queryClient }]] } });
    const dialog = wrapper.find("dialog").element;
    dialog.showModal = vi.fn(() => (dialog.open = true));
    dialog.close = vi.fn((value = "") => {
      dialog.returnValue = value;
      dialog.open = false;
      dialog.dispatchEvent(new Event("close"));
    });
    await flushPromises();
  }

  async function confirm(): Promise<void> {
    await wrapper.find("dialog .primary-button").trigger("click");
    await flushPromises();
  }

  async function selectSource(text: string): Promise<void> {
    await wrapper
      .findAll(".plugin-source-switch button")
      .find((button) => button.text() === text)!
      .trigger("click");
  }

  it("lists package versions and only offers deletion for removable plugins", async () => {
    await render();
    const rows = wrapper.findAll(".plugin-list li");
    expect(rows[0]!.text()).toContain("内置");
    expect(rows[0]!.findAll("button")).toHaveLength(0);
    expect(rows[1]!.text()).toContain("0.1.0");
    expect(rows[1]!.find("button").text()).toBe("删除");
  });

  it("installs a GitHub repository after confirmation and shows the restart notice", async () => {
    await render();
    await wrapper.find("input[type=url]").setValue("https://github.com/example/course2md-demo");
    await wrapper.find("form").trigger("submit");
    expect(client.POST).not.toHaveBeenCalled();
    expect(wrapper.find("dialog h2").text()).toBe("安装插件");
    await confirm();
    expect(client.POST).toHaveBeenCalledWith("/api/v1/plugins", {
      body: { source: "https://github.com/example/course2md-demo", kind: "github" },
    });
    expect(wrapper.find(".plugin-restart").text()).toContain("重启");
  });

  it("submits a local project path", async () => {
    await render();
    await selectSource("本地目录 / 路径");
    await wrapper.find("form input").setValue("D:/plugins/course2md-demo");
    await wrapper.find("form").trigger("submit");
    await confirm();
    expect(client.POST).toHaveBeenCalledWith("/api/v1/plugins", {
      body: { source: "D:/plugins/course2md-demo", kind: "local" },
    });
  });

  it("uploads the selected package as binary content", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify(listing({ restart_required: true, message: "插件已安装" }))),
      );
    vi.stubGlobal("fetch", fetchMock);
    await render();
    await selectSource("本地安装包");
    const file = new File(["wheel-data"], "plugin.whl");
    const input = wrapper.find("input[type=file]");
    Object.defineProperty(input.element, "files", { value: [file] });
    await input.trigger("change");
    await wrapper.find("form").trigger("submit");
    await confirm();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/plugins/import?filename=plugin.whl", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
  });

  it("does not delete until confirmed, and preserves the builtin resolver", async () => {
    await render();
    await wrapper.find(".plugin-list button").trigger("click");
    await wrapper.find("dialog .quiet-button").trigger("click");
    expect(client.DELETE).not.toHaveBeenCalled();
    await wrapper.find(".plugin-list button").trigger("click");
    await confirm();
    expect(client.DELETE).toHaveBeenCalledWith("/api/v1/plugins/{name}", {
      params: { path: { name: "course2md-demo" } },
    });
    expect(wrapper.findAll(".plugin-list li")).toHaveLength(1);
    expect(wrapper.find(".plugin-list").text()).toContain("本地视频");
  });

  it("shows installation failures without losing the plugin list", async () => {
    vi.mocked(client.POST).mockRejectedValue(new Error("安装来源缺少插件入口点"));
    await render();
    await wrapper.find("input[type=url]").setValue("https://github.com/example/other");
    await wrapper.find("form").trigger("submit");
    await confirm();
    expect(ui.error).toBe("安装来源缺少插件入口点");
    expect(wrapper.findAll(".plugin-list li")).toHaveLength(2);
    expect(wrapper.find("form .primary-button").attributes("disabled")).toBeUndefined();
  });

  it("keeps the server view read-only", async () => {
    session.mode = "server";
    vi.mocked(client.GET).mockResolvedValue({
      data: listing({
        can_manage: false,
        plugins: listing().plugins.map((plugin) => ({ ...plugin, removable: false })),
      }),
      response: new Response(),
    } as never);
    await render();
    expect(wrapper.find("form").exists()).toBe(false);
    expect(wrapper.findAll(".plugin-list button")).toHaveLength(0);
    expect(wrapper.text()).toContain("COURSE2MD_PLUGINS");
  });
});
