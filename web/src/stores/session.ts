import { reactive } from "vue";

import { client, type Config, resetApiSession, unwrap, type UserInfo } from "@/api/client";
import { resetJobs } from "./jobs";
import { queryClient } from "./query";
import { ui } from "./ui";

/** 运行环境：本机配置、登录状态与「整理选项」草稿（新建任务时使用）。 */
export const session = reactive({
  mode: "local" as "local" | "server",
  registration: "closed",
  hasUsers: true,
  user: null as UserInfo | null,
  config: null as Config | null,
});

const defaultOptions = () => ({
  outDir: "",
  model: "large-v3",
  device: "cuda",
  formats: ["srt", "txt", "md"] as string[],
  views: [] as number[],
  refine: true,
  proofread: true,
  frames: true,
  frameSelector: "",
  detail: true,
  keepAudio: false,
  deleteVideo: true,
  llmBase: "",
  llmModel: "",
  agent: "",
  agentChoices: {} as Record<string, { provider: string; model: string }>,
});
export const options = reactive(defaultOptions());

/** 清除账号数据，并拒绝旧账号尚未返回的 HTTP / SSE 响应。 */
export function resetSession(): void {
  resetApiSession();
  resetJobs();
  queryClient.clear();
  session.user = null;
  session.config = null;
  Object.assign(options, defaultOptions());
  ui.error = "";
  ui.toast = "";
}

export async function loadStatus(): Promise<void> {
  const status = await unwrap(client.GET("/api/v1/auth/status"));
  session.mode = status.mode === "server" ? "server" : "local";
  session.registration = status.registration ?? "closed";
  session.hasUsers = status.has_users ?? true;
}

export async function loadUser(): Promise<UserInfo | null> {
  if (session.mode === "local") {
    session.user = { id: "local", username: "本机", role: "admin", disabled: false };
    return session.user;
  }
  const { data, response } = await client.GET("/api/v1/auth/me");
  session.user = response.ok && data ? data.user : null;
  return session.user;
}

export async function loadConfig(): Promise<Config> {
  const config = await unwrap(client.GET("/api/v1/config"));
  session.config = config;
  Object.assign(options, {
    outDir: config.default_out ?? "",
    model: config.model,
    device: config.device,
    formats: [...(config.default_formats ?? [])],
    proofread: config.proofread,
    frames: config.frames,
    frameSelector: config.frame_selector ?? "",
    detail: config.detail,
    deleteVideo: config.delete_video,
    llmBase: config.llm_base,
    llmModel: config.llm_model,
    agent: config.agent_default ?? "",
  });
  for (const [name, choice] of Object.entries(config.agent_choices ?? {})) {
    options.agentChoices[name] = { provider: choice.provider ?? "", model: choice.model ?? "" };
  }
  return config;
}

export const isAdmin = () => session.user?.role === "admin";
