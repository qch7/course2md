import { describe, expect, it } from "vitest";

import type { LocalNode, Worker } from "@/api/client";

import {
  hubHost,
  linkDotClass,
  linkMeshStatus,
  localNodeStatus,
  parseWorkerCommand,
  workerStatus,
} from "./mesh";

describe("状态映射", () => {
  it("外部 Hub 连接状态映射到拓扑图状态", () => {
    expect(linkMeshStatus("online")).toBe("on");
    expect(linkMeshStatus("busy")).toBe("busy");
    expect(linkMeshStatus("connecting")).toBe("warn");
    expect(linkMeshStatus("reconnecting")).toBe("warn");
    expect(linkMeshStatus("auth_failed")).toBe("err");
    expect(linkMeshStatus("disconnected")).toBe("off");
    expect(linkMeshStatus(undefined)).toBe("off");
  });

  it("列表圆点与拓扑图用同一套规则，未连接时不带类名", () => {
    for (const status of ["online", "busy", "connecting", "reconnecting", "auth_failed"]) {
      expect(linkDotClass(status)).toBe(linkMeshStatus(status));
    }
    expect(linkDotClass("disconnected")).toBe("");
    expect(linkDotClass()).toBe("");
  });

  it("本机节点：停用或离线为 off，执行中为 busy", () => {
    expect(localNodeStatus({ enabled: false, online: true } as LocalNode)).toBe("off");
    expect(localNodeStatus({ enabled: true, online: false } as LocalNode)).toBe("off");
    expect(localNodeStatus({ enabled: true, online: true, busy: true } as LocalNode)).toBe("busy");
    expect(localNodeStatus({ enabled: true, online: true, busy: false } as LocalNode)).toBe("on");
  });

  it("远程节点只看是否在线", () => {
    expect(workerStatus({ online: true } as Worker)).toBe("on");
    expect(workerStatus({ online: false } as Worker)).toBe("off");
  });
});

describe("hubHost", () => {
  it("取主机和端口，无法解析时原样返回", () => {
    expect(hubHost("https://hub.example.com:8443/path")).toBe("hub.example.com:8443");
    expect(hubHost("not a url")).toBe("not a url");
    expect(hubHost(undefined)).toBe("");
  });
});

describe("parseWorkerCommand", () => {
  it("解析带引号和等号的各个参数", () => {
    const parsed = parseWorkerCommand(
      `course2md worker --hub https://h.example --token="abc 123" --name 'lab pc' --work-dir=D:/work`,
    );
    expect(parsed).toEqual({
      hub_url: "https://h.example",
      token: "abc 123",
      name: "lab pc",
      work_dir: "D:/work",
    });
  });

  it("不像节点命令时返回 null", () => {
    expect(parseWorkerCommand("https://h.example")).toBeNull();
    expect(parseWorkerCommand("--hub")).toBeNull();
  });
});
