import { reactive } from "vue";

import { client, type Course, type Job, unwrap } from "@/api/client";

/** 全部任务与课程。任务状态通过 SSE 增量更新（只推送新的日志与运行输出）。 */
export const store = reactive({
  jobs: new Map<string, Job>(),
  courses: new Map<string, Course>(),
  loaded: false,
});

export function isBusy(job: Job | undefined): boolean {
  return !!job && (["queued", "running"].includes(job.status ?? "") || (job.pending_messages ?? 0) > 0);
}

/** 合并增量快照：日志按偏移拼接，运行输出按序号去重，最多保留最近 500 / 400 条。 */
export function mergeJob(previous: Job | undefined, data: Job): Job {
  const logs = data.logs ?? [];
  const logStart = (data.log_offset ?? 0) - logs.length;
  const skip = Math.max(0, (previous?.log_offset ?? 0) - logStart);
  const events = new Map((previous?.events ?? []).map((event) => [event.seq, event]));
  (data.events ?? []).forEach((event) => events.set(event.seq, event));
  return {
    ...previous,
    ...data,
    logs: [...(previous?.logs ?? []), ...logs.slice(skip)].slice(-500),
    log_offset: Math.max(previous?.log_offset ?? 0, data.log_offset ?? 0),
    events: [...events.values()].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0)).slice(-400),
    event_offset: Math.max(previous?.event_offset ?? 0, data.event_offset ?? 0),
  };
}

export function absorb(data: Job): Job {
  const merged = mergeJob(store.jobs.get(data.id), data);
  store.jobs.set(data.id, merged);
  return merged;
}

export function absorbCourse(course: Course): void {
  store.courses.set(course.id, course);
}

export function dropJob(id: string): void {
  store.jobs.delete(id);
  stopWatching(id);
}

export async function loadAll(): Promise<void> {
  const epoch = generation;
  const [jobs, courses] = await Promise.all([
    unwrap(client.GET("/api/v1/jobs")),
    unwrap(client.GET("/api/v1/courses")),
  ]);
  if (epoch !== generation) return;
  resetJobs();
  courses.courses?.forEach(absorbCourse);
  jobs.jobs?.forEach(absorb);
  store.loaded = true;
  store.jobs.forEach((job) => {
    if (isBusy(job)) watchJob(job.id);
  });
}

// ------------------------------------------------------------------ 订阅
type Watcher = { source?: EventSource; timer?: ReturnType<typeof setTimeout>; stopped: boolean };
const watchers = new Map<string, Watcher>();
let selected: string | null = null;
let generation = 0;

export function resetJobs(): void {
  generation += 1;
  for (const id of watchers.keys()) stopWatching(id);
  selected = null;
  store.jobs.clear();
  store.courses.clear();
  store.loaded = false;
}

export function setSelected(id: string | null): void {
  selected = id;
  if (id) watchJob(id);
}

export function stopWatching(id: string): void {
  const state = watchers.get(id);
  if (!state) return;
  state.stopped = true;
  state.source?.close();
  clearTimeout(state.timer);
  watchers.delete(id);
}

/** 订阅任务事件；连接失败时 3 秒后重连，任务结束后停止（当前选中的任务稍后再检查一次）。 */
export function watchJob(id: string): void {
  if (watchers.has(id) || typeof EventSource === "undefined") return;
  const state: Watcher = { stopped: false };
  watchers.set(id, state);
  const connect = () => {
    const job = store.jobs.get(id);
    if (!job || state.stopped) return;
    const query = `since=${job.log_offset ?? 0}&after=${job.event_offset ?? 0}`;
    const source = new EventSource(`/api/v1/jobs/${encodeURIComponent(id)}/events?${query}`);
    state.source = source;
    source.addEventListener("job", (event) => {
      if (!state.stopped) absorb(JSON.parse((event as MessageEvent).data) as Job);
    });
    source.addEventListener("end", () => {
      source.close();
      if (selected === id && !state.stopped) state.timer = setTimeout(connect, 4000);
      else stopWatching(id);
    });
    source.onerror = () => {
      source.close();
      if (!state.stopped) state.timer = setTimeout(connect, 3000);
    };
  };
  connect();
}
