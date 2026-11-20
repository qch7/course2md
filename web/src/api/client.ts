import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

type Schemas = components["schemas"];
export type Job = Schemas["Job"];
export type Course = Schemas["Course"];
export type Session = Schemas["Session"];
export type Config = Schemas["Config"];
export type Worker = Schemas["Worker"];
export type Task = Schemas["Task"];
export type NoteVersion = Schemas["NoteVersion"];
export type UserInfo = Schemas["UserInfo"];
export type FileEntry = Schemas["FileEntry"];
export type JobEvent = Schemas["JobEvent"];
export type CreateJobRequest = Schemas["CreateJobRequest"];
export type Plugin = Schemas["Plugin"];
export type PluginsResponse = Schemas["PluginsResponse"];

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public payload: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

export const client = createClient<paths>({ baseUrl: "" });
let sessionEpoch = 0;
const requestEpochs = new WeakMap<Request, number>();

export function resetApiSession(): void {
  sessionEpoch += 1;
}

client.use({
  onRequest({ request }) {
    requestEpochs.set(request, sessionEpoch);
  },
  onResponse({ request, response }) {
    if (requestEpochs.get(request) !== sessionEpoch) throw new Error("登录状态已变更，请重试");
    if (response.status === 401) window.dispatchEvent(new CustomEvent("course2md:unauthorized"));
    return response;
  },
});

/** 解包 openapi-fetch 的结果：成功返回数据，失败抛出带中文说明的 ApiError。 */
export async function unwrap<T>(
  request: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await request;
  if (response.ok && data !== undefined) return data;
  const payload = (error && typeof error === "object" ? error : {}) as Record<string, unknown>;
  const message = typeof payload.error === "string" ? payload.error : `请求失败（HTTP ${response.status}）`;
  throw new ApiError(message, response.status, payload);
}

export function fileUrl(jobId: string, rel: string, inline = false): string {
  const query = `rel=${encodeURIComponent(rel)}${inline ? "&inline=1" : ""}`;
  return `/api/v1/jobs/${encodeURIComponent(jobId)}/file?${query}`;
}

export async function importPlugin(file: File): Promise<PluginsResponse> {
  const response = await fetch(`/api/v1/plugins/import?filename=${encodeURIComponent(file.name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  const payload = await response.json();
  return unwrap<PluginsResponse>(
    Promise.resolve({
      data: response.ok ? payload : undefined,
      error: response.ok ? undefined : payload,
      response,
    }),
  );
}
