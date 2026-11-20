export const agentNames: Record<string, string> = {
  dsh: "DeepSeek Harness",
  codex: "Codex",
  builtin: "内置模型",
  custom: "自定义 Agent",
};

export const statusNames: Record<string, string> = {
  queued: "排队中",
  running: "整理中",
  done: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
  pending: "等待节点",
  leased: "执行中",
};

export const stageNames: Record<string, string> = {
  media: "转写",
  frames: "画面",
  proofread: "校对",
  output: "输出",
  organize: "整理",
};

export function agentName(name: string | undefined): string {
  return agentNames[name ?? ""] ?? name ?? "";
}

export function backendLabel(job: { agent?: string; agent_model?: string }): string {
  const name = agentName(job.agent);
  return job.agent_model ? `${name} · ${job.agent_model}` : name;
}

export function sessionLabel(job: { course?: string; title?: string }): string {
  const course = job.course ?? "";
  const title = job.title ?? "";
  if (course && title.startsWith(course)) {
    const rest = title.slice(course.length).trim();
    if (rest) return rest;
  }
  return title;
}

export function dateTime(seconds: number | null | undefined): string {
  if (!seconds) return "";
  return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

export function duration(start?: number | null, end?: number | null): string {
  if (!start || !end) return "";
  const seconds = Math.max(0, end - start);
  return seconds >= 60
    ? `${Math.floor(seconds / 60)} 分 ${Math.round(seconds % 60)} 秒`
    : `${seconds.toFixed(1)} 秒`;
}
