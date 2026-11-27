/** 节点能力的中文名称与分组，计算服务页的节点卡片、详情弹窗和本机节点编辑共用。 */

const capabilityNames: Record<string, string> = {
  asr: "转写",
  frames: "画面",
  "frames:decode": "画面解码",
  "llm-api": "大模型接口",
  hub: "Hub 本机",
  node: "独立节点",
  "agent:dsh": "DSH",
  "agent:codex": "Codex",
  "agent:custom": "自定义 Agent",
  "frame-selector:general": "通用画面筛选",
  "frame-selector:classroom": "课堂画面筛选",
};

const capabilityHints: Record<string, string> = {
  asr: "已安装 faster-whisper 与 PyAV，可抽取音频并转写",
  frames: "已安装 PyAV 与 OpenCV，可扫描并筛选画面",
  "frames:decode": "已安装 PyAV，可解码视频画面",
  "llm-api": "已配置 OpenAI 兼容接口密钥，可做校对与整理",
  hub: "与 Hub 同机运行，可直接读取本机文件与下载的视频",
  "agent:custom": "可运行自定义整理命令",
};

export interface CapabilityGroup {
  key: string;
  title: string;
  items: { name: string; label: string; hint: string }[];
}

const GROUPS: { key: string; title: string; match: (name: string) => boolean }[] = [
  {
    key: "media",
    title: "转写与画面",
    match: (name) => name === "asr" || name.startsWith("frames") || name.startsWith("frame-selector:"),
  },
  {
    key: "organize",
    title: "校对与整理",
    match: (name) => name === "llm-api" || name.startsWith("agent:"),
  },
  { key: "source", title: "来源解析", match: (name) => name.startsWith("resolver:") },
  { key: "host", title: "运行位置", match: () => true },
];

export function capabilityLabel(capability: string): string {
  if (capability.startsWith("resolver:")) return `来源：${capability.slice(9)}`;
  if (capability.startsWith("frame-selector:") && !capabilityNames[capability]) {
    return `画面筛选：${capability.slice(15)}`;
  }
  return capabilityNames[capability] ?? capability;
}

export function capabilityHint(capability: string): string {
  return capabilityHints[capability] ?? "";
}

/** 把能力名按用途分组，空组不返回；组内按显示名排序，保持列表稳定。 */
export function groupCapabilities(capabilities: readonly string[]): CapabilityGroup[] {
  const groups = GROUPS.map((group) => ({
    key: group.key,
    title: group.title,
    items: [] as CapabilityGroup["items"],
  }));
  for (const name of [...new Set(capabilities)].sort()) {
    const index = GROUPS.findIndex((group) => group.match(name));
    groups[index]!.items.push({ name, label: capabilityLabel(name), hint: capabilityHint(name) });
  }
  return groups.filter((group) => group.items.length > 0);
}
