// 计算服务页用到的纯函数：状态映射、主机名与节点命令解析。不依赖组件状态，便于单独测试。
import type { LocalNode, Worker } from "@/api/client";
import type { MeshStatus } from "@/components/MeshTopology.vue";

export const linkStatusLabels: Record<string, string> = {
  online: "在线",
  busy: "执行中",
  connecting: "连接中…",
  reconnecting: "重连中…",
  auth_failed: "令牌失效",
  disconnected: "未连接",
};

export function workerStatus(worker: Worker): MeshStatus {
  return worker.online ? "on" : "off";
}

export function localNodeStatus(node: LocalNode): MeshStatus {
  if (!node.enabled || !node.online) return "off";
  return node.busy ? "busy" : "on";
}

export function linkMeshStatus(status?: string): MeshStatus {
  if (status === "online") return "on";
  if (status === "busy") return "busy";
  if (status === "connecting" || status === "reconnecting") return "warn";
  if (status === "auth_failed") return "err";
  return "off";
}

// 列表里状态圆点的样式类；与 linkMeshStatus 同一套规则，未连接时不带类名
export function linkDotClass(status?: string): string {
  const mapped = linkMeshStatus(status);
  return mapped === "off" ? "" : mapped;
}

export function hubHost(url?: string): string {
  if (!url) return "";
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

export interface ParsedWorkerCommand {
  hub_url?: string;
  token?: string;
  name?: string;
  work_dir?: string;
}

const optionPattern = (flag: string): RegExp => new RegExp(`--${flag}(?:=|\\s+)([^\\s"']+|"[^"]+"|'[^']+')`);

// 把粘贴进来的 `course2md worker --hub ... --token ...` 命令拆成各字段；不像节点命令时返回 null
export function parseWorkerCommand(raw: string): ParsedWorkerCommand | null {
  if (!raw.includes("--hub") && !raw.includes("--token")) return null;
  const hubMatch = raw.match(optionPattern("hub"));
  const tokenMatch = raw.match(optionPattern("token"));
  if (!hubMatch && !tokenMatch) return null;
  const unquote = (value?: string) => (value ? value.replace(/^["']|["']$/g, "") : undefined);
  return {
    hub_url: unquote(hubMatch?.[1]),
    token: unquote(tokenMatch?.[1]),
    name: unquote(raw.match(optionPattern("name"))?.[1]),
    work_dir: unquote(raw.match(optionPattern("work-dir"))?.[1]),
  };
}
