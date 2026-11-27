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
  items: CapabilityItem[];
}

export interface CapabilityItem {
  /** 主能力名；带版本的 `resolver:<名称>@<版本>` 会并入对应的 `resolver:<名称>` */
  name: string;
  /** 这一项实际代表的全部能力名，勾选时要一起增减 */
  names: string[];
  label: string;
  hint: string;
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

/**
 * 把能力名按用途分组，空组不返回；组内按显示名排序，保持列表稳定。
 * 后端会为每个来源解析器同时声明 `resolver:hdu` 与 `resolver:hdu@0.1.0`（后者供任务指定版本），
 * 对用户来说是同一个解析器，所以合并成一项，版本号写进说明。
 */
export function groupCapabilities(capabilities: readonly string[]): CapabilityGroup[] {
  const groups = GROUPS.map((group) => ({
    key: group.key,
    title: group.title,
    items: [] as CapabilityItem[],
  }));
  const present = new Set(capabilities);
  for (const name of [...present].sort()) {
    const [base, version] = name.split("@", 2) as [string, string | undefined];
    if (version !== undefined && present.has(base)) continue; // 并入不带版本的那一项
    const index = GROUPS.findIndex((group) => group.match(name));
    const versions = [...present].filter((other) => other.startsWith(`${name}@`));
    const versionText = versions.map((other) => `v${other.slice(name.length + 1)}`).join("、");
    groups[index]!.items.push({
      name,
      names: [name, ...versions],
      label: versionText ? `${capabilityLabel(name)} ${versionText}` : capabilityLabel(name),
      hint: capabilityHint(name),
    });
  }
  return groups.filter((group) => group.items.length > 0);
}

export interface CapabilityTag {
  label: string;
  /** 聚合标签悬停时列出包含的全部能力 */
  title: string;
}

/**
 * 节点卡片里的能力摘要：来源解析与画面筛选通常有很多条，合并成「来源 ×3」这样的聚合标签，
 * 其余能力单独列出；完整列表在详情弹窗里看。
 */
export function summarizeCapabilities(capabilities: readonly string[]): CapabilityTag[] {
  const tags: CapabilityTag[] = [];
  for (const group of groupCapabilities(capabilities)) {
    const merged = group.key === "source" || group.key === "media" ? "merge" : "single";
    const singles = group.items.filter(
      (item) =>
        !(
          merged === "merge" &&
          (item.name.startsWith("resolver:") || item.name.startsWith("frame-selector:"))
        ),
    );
    for (const item of singles) tags.push({ label: item.label, title: item.hint || item.name });
    const selectors = group.items.filter((item) => item.name.startsWith("frame-selector:"));
    if (selectors.length) {
      tags.push({
        label: `画面筛选 ×${selectors.length}`,
        title: selectors.map((item) => item.label).join("、"),
      });
    }
    // 带版本号的 resolver:<名称>@<版本> 与不带版本的是同一个解析器，只计一次
    const resolvers = [
      ...new Set(
        group.items
          .filter((item) => item.name.startsWith("resolver:"))
          .map((item) => item.name.split("@")[0]!),
      ),
    ];
    if (resolvers.length) {
      tags.push({
        label: `来源解析 ×${resolvers.length}`,
        title: resolvers.map((name) => name.slice(9)).join("、"),
      });
    }
  }
  return tags;
}
