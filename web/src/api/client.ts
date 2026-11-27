import createClient from "openapi-fetch";

import type { components, paths } from "./schema";

type Schemas = components["schemas"];
export type Job = Schemas["Job"];
export type Course = Schemas["Course"];
export type Session = Schemas["Session"];
export type Config = Schemas["Config"];
export type Worker = Schemas["Worker"];
export type HubLink = Schemas["HubLink"];
export type HubLinksResponse = Schemas["HubLinksResponse"];
export type LocalNode = Schemas["LocalNode"];
export type LocalNodesResponse = Schemas["LocalNodesResponse"];
export type Task = Schemas["Task"];
export type NoteVersion = Schemas["NoteVersion"];
export type UserInfo = Schemas["UserInfo"];
export type FileEntry = Schemas["FileEntry"];
export type JobEvent = Schemas["JobEvent"];
export type CreateJobRequest = Schemas["CreateJobRequest"];
export type Plugin = Schemas["Plugin"];
export type PluginsResponse = Schemas["PluginsResponse"];
export type TransferPeer = Schemas["TransferPeer"];
export type TransferPeersResponse = Schemas["TransferPeersResponse"];
export type RemoteCourse = Schemas["RemoteCourse"];
export type RemoteJob = Schemas["RemoteJob"];
export type RemoteCatalogResponse = Schemas["RemoteCatalogResponse"];
export type BundleImportResponse = Schemas["BundleImportResponse"];
export type TransferResultResponse = Schemas["TransferResultResponse"];

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

const WRITE_METHODS = new Set(["POST", "PUT", "PATCH"]);

client.use({
  onRequest({ request }) {
    requestEpochs.set(request, sessionEpoch);
    // 服务端要求写请求声明 application/json（跨站表单无法伪造，是 CSRF 防线之一）。
    // 停止任务、退出登录这类没有请求体的 POST，openapi-fetch 不会带 Content-Type，需要补上。
    if (WRITE_METHODS.has(request.method) && !request.headers.has("content-type")) {
      request.headers.set("content-type", "application/json");
    }
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

export function jobBundleUrl(jobId: string, includeCache = false): string {
  const query = includeCache ? "?include_cache=true" : "";
  return `/api/v1/jobs/${encodeURIComponent(jobId)}/bundle${query}`;
}

export function courseBundleUrl(courseId: string, jobIds?: string[], includeCache = false): string {
  const params: string[] = [];
  if (jobIds?.length) params.push(`job_ids=${encodeURIComponent(jobIds.join(","))}`);
  if (includeCache) params.push("include_cache=true");
  return `/api/v1/courses/${encodeURIComponent(courseId)}/bundle${params.length ? `?${params.join("&")}` : ""}`;
}

export async function importBundle(
  file: File,
  conflict: "skip" | "overwrite" | "copy" = "skip",
): Promise<BundleImportResponse> {
  const response = await fetch(`/api/v1/bundles/import?conflict=${encodeURIComponent(conflict)}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  const payload = await response.json();
  return unwrap<BundleImportResponse>(
    Promise.resolve({
      data: response.ok ? payload : undefined,
      error: response.ok ? undefined : payload,
      response,
    }),
  );
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
