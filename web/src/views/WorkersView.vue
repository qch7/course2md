<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, reactive, ref } from "vue";

import { client, type HubLink, unwrap, type Worker } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import { isAdmin, session } from "@/stores/session";
import { showError, toast, ui } from "@/stores/ui";
import { dateTime } from "@/utils/format";

const workers = useQuery({
  queryKey: ["workers"],
  queryFn: () => unwrap(client.GET("/api/v1/workers")),
  refetchInterval: 10000,
});
const hubLinks = useQuery({
  queryKey: ["hub-links"],
  queryFn: () => unwrap(client.GET("/api/v1/hub-links")),
  enabled: computed(() => session.mode === "local" || isAdmin()),
  refetchInterval: 5000,
});

const name = ref("");
const personal = ref(false);
const removing = ref<string | null>(null);
const confirmation = ref<InstanceType<typeof ConfirmDialog> | null>(null);
const created = ref<{ worker_id: string; token: string; name: string; shared: boolean } | null>(null);

const showAddHub = ref(false);
const busyLink = ref<string | null>(null);
const expandedLogs = reactive<Record<string, boolean>>({});
const editingId = ref<string | null>(null);

const newLink = reactive({
  hub_url: "",
  token: "",
  name: "",
  work_dir: "",
  auto_connect: true,
  no_agents: false,
  no_asr: false,
});

const editForm = reactive({
  hub_url: "",
  token: "",
  name: "",
  work_dir: "",
  auto_connect: true,
  no_agents: false,
  no_asr: false,
});

const hubUrl = computed(() => location.origin);
const canManageLinks = computed(() => Boolean(hubLinks.data.value?.can_manage));
const outgoingLinks = computed(() => hubLinks.data.value?.links ?? []);
const incomingWorkers = computed(() => workers.data.value?.workers ?? []);
const incomingOnlineCount = computed(() => incomingWorkers.value.filter((item) => item.online).length);
const outgoingOnlineCount = computed(
  () => outgoingLinks.value.filter((item) => item.status === "online" || item.status === "busy").length,
);

const capabilityNames: Record<string, string> = {
  asr: "转写",
  frames: "画面",
  "llm-api": "大模型接口",
  hub: "Hub 本机",
  node: "独立节点",
  "agent:dsh": "DSH",
  "agent:codex": "Codex",
  "agent:custom": "自定义 Agent",
};

const linkStatusLabels: Record<string, string> = {
  online: "在线",
  busy: "执行中",
  connecting: "连接中…",
  reconnecting: "重连中…",
  auth_failed: "令牌失效",
  disconnected: "未连接",
};

function label(capability: string): string {
  if (capability.startsWith("resolver:")) return `来源：${capability.slice(9)}`;
  return capabilityNames[capability] ?? capability;
}

function linkDotClass(status?: string): string {
  if (status === "online") return "on";
  if (status === "busy") return "busy";
  if (status === "connecting" || status === "reconnecting") return "warn";
  if (status === "auth_failed") return "err";
  return "";
}

function hubHost(url?: string): string {
  if (!url) return "";
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

function parseWorkerCommand(
  raw: string,
): { hub_url?: string; token?: string; name?: string; work_dir?: string } | null {
  if (!raw.includes("--hub") && !raw.includes("--token")) return null;
  const hubMatch = raw.match(/--hub(?:=|\s+)([^\s"']+|"[^"]+"|'[^']+')/);
  const tokenMatch = raw.match(/--token(?:=|\s+)([^\s"']+|"[^"]+"|'[^']+')/);
  if (!hubMatch && !tokenMatch) return null;
  const unquote = (value?: string) => (value ? value.replace(/^["']|["']$/g, "") : undefined);
  const nameMatch = raw.match(/--name(?:=|\s+)([^\s"']+|"[^"]+"|'[^']+')/);
  const dirMatch = raw.match(/--work-dir(?:=|\s+)([^\s"']+|"[^"]+"|'[^']+')/);
  return {
    hub_url: unquote(hubMatch?.[1]),
    token: unquote(tokenMatch?.[1]),
    name: unquote(nameMatch?.[1]),
    work_dir: unquote(dirMatch?.[1]),
  };
}

function handleHubInput(target: { hub_url: string; token: string; name: string; work_dir: string }): void {
  const parsed = parseWorkerCommand(target.hub_url);
  if (!parsed) return;
  if (parsed.hub_url) target.hub_url = parsed.hub_url;
  if (parsed.token) target.token = parsed.token;
  if (parsed.name) target.name = parsed.name;
  if (parsed.work_dir) target.work_dir = parsed.work_dir;
  toast("已从命令自动填入 Hub 地址与令牌");
}

function canManage(worker: Worker): boolean {
  return (
    worker.kind !== "builtin" &&
    (session.mode === "local" || isAdmin() || worker.owner_id === session.user?.id)
  );
}

async function create(): Promise<void> {
  try {
    const data = await unwrap(
      client.POST("/api/v1/workers", { body: { name: name.value || "计算节点", personal: personal.value } }),
    );
    created.value = {
      worker_id: data.worker_id,
      token: data.token ?? "",
      name: data.name ?? "",
      shared: !!data.shared,
    };
    name.value = "";
    workers.refetch();
  } catch (error) {
    showError(error);
  }
}

async function disable(id: string, workerLabel: string): Promise<void> {
  if (
    !(await confirmation.value?.open({
      title: "停用节点",
      message: `确定停用节点「${workerLabel}」？`,
      detail: "其令牌将立即作废，节点记录会保留。",
      confirmText: "停用节点",
    }))
  )
    return;
  try {
    await unwrap(client.DELETE("/api/v1/workers/{worker_id}", { params: { path: { worker_id: id } } }));
    toast("节点已停用");
    workers.refetch();
  } catch (error) {
    showError(error);
  }
}

async function remove(id: string, workerLabel: string): Promise<void> {
  if (
    !(await confirmation.value?.open({
      title: "删除节点",
      message: `确定删除节点「${workerLabel}」？`,
      detail: "其令牌将立即作废，重新连接需要添加新节点。课程、笔记和历史记录会保留。",
      confirmText: "删除节点",
    }))
  )
    return;
  removing.value = id;
  try {
    await unwrap(
      client.DELETE("/api/v1/workers/{worker_id}", {
        params: { path: { worker_id: id }, query: { permanent: true } },
      }),
    );
    if (created.value?.worker_id === id) created.value = null;
    toast("节点已删除");
    await workers.refetch();
  } catch (error) {
    showError(error);
  } finally {
    removing.value = null;
  }
}

async function addHubLink(): Promise<void> {
  busyLink.value = "new";
  try {
    await unwrap(
      client.POST("/api/v1/hub-links", {
        body: {
          hub_url: newLink.hub_url.trim(),
          token: newLink.token.trim(),
          name: newLink.name.trim(),
          work_dir: newLink.work_dir.trim(),
          auto_connect: newLink.auto_connect,
          no_agents: newLink.no_agents,
          without: newLink.no_asr ? ["asr"] : [],
          connect_now: true,
        },
      }),
    );
    newLink.hub_url = "";
    newLink.token = "";
    newLink.name = "";
    newLink.work_dir = "";
    newLink.auto_connect = true;
    newLink.no_agents = false;
    newLink.no_asr = false;
    showAddHub.value = false;
    toast("已添加并开始连接远程 Hub");
    await hubLinks.refetch();
  } catch (error) {
    showError(error);
  } finally {
    busyLink.value = null;
  }
}

function startEdit(link: HubLink): void {
  editingId.value = link.id;
  editForm.hub_url = link.hub_url ?? "";
  editForm.token = "";
  editForm.name = link.name ?? "";
  editForm.work_dir = link.work_dir ?? "";
  editForm.auto_connect = link.auto_connect ?? true;
  editForm.no_agents = link.no_agents ?? false;
  editForm.no_asr = (link.without ?? []).includes("asr");
}

async function saveEdit(linkId: string): Promise<void> {
  busyLink.value = linkId;
  try {
    const body: Record<string, unknown> = {
      hub_url: editForm.hub_url.trim(),
      name: editForm.name.trim(),
      auto_connect: editForm.auto_connect,
      no_agents: editForm.no_agents,
      without: editForm.no_asr ? ["asr"] : [],
    };
    if (editForm.token.trim()) body.token = editForm.token.trim();
    if (session.mode === "local") body.work_dir = editForm.work_dir.trim();
    await unwrap(
      client.PATCH("/api/v1/hub-links/{link_id}", {
        params: { path: { link_id: linkId } },
        body,
      }),
    );
    editingId.value = null;
    toast("已保存连接配置");
    await hubLinks.refetch();
  } catch (error) {
    showError(error);
  } finally {
    busyLink.value = null;
  }
}

async function connectLink(linkId: string): Promise<void> {
  busyLink.value = linkId;
  try {
    await unwrap(
      client.POST("/api/v1/hub-links/{link_id}/connect", {
        params: { path: { link_id: linkId } },
      }),
    );
    toast("正在连接远程 Hub");
    await hubLinks.refetch();
  } catch (error) {
    showError(error);
  } finally {
    busyLink.value = null;
  }
}

async function disconnectLink(link: HubLink): Promise<void> {
  if (link.status === "busy") {
    const confirmed = await confirmation.value?.open({
      title: "断开远程 Hub",
      message: `节点正在为 ${hubHost(link.hub_url)} 执行「${link.current_task?.stage ?? "任务"}」，确定立即断开？`,
      detail: "当前阶段会归还给远程 Hub 等待重新派发。",
      confirmText: "立即断开",
    });
    if (!confirmed) return;
  }
  busyLink.value = link.id;
  try {
    await unwrap(
      client.POST("/api/v1/hub-links/{link_id}/disconnect", {
        params: { path: { link_id: link.id } },
      }),
    );
    toast("已断开远程 Hub");
    await hubLinks.refetch();
  } catch (error) {
    showError(error);
  } finally {
    busyLink.value = null;
  }
}

async function deleteLink(link: HubLink): Promise<void> {
  const confirmed = await confirmation.value?.open({
    title: "移除外部 Hub",
    message: `确定移除与 ${hubHost(link.hub_url)} 的连接？`,
    detail: "会断开当前连接并移除本机保存的令牌；远程 Hub 上的历史课程与笔记不受影响。",
    confirmText: "移除连接",
  });
  if (!confirmed) return;
  busyLink.value = link.id;
  try {
    await unwrap(
      client.DELETE("/api/v1/hub-links/{link_id}", {
        params: { path: { link_id: link.id } },
      }),
    );
    if (editingId.value === link.id) editingId.value = null;
    toast("已移除外部 Hub 连接");
    await hubLinks.refetch();
  } catch (error) {
    showError(error);
  } finally {
    busyLink.value = null;
  }
}

async function copy(text: string): Promise<void> {
  await navigator.clipboard.writeText(text);
  toast("已复制");
}
</script>

<template>
  <header class="topbar">
    <button
      class="icon-button mobile-menu"
      aria-label="展开课程列表"
      @click="ui.sidebarOpen = !ui.sidebarOpen"
    >
      ☰
    </button>
    <div class="course-heading"><h1>计算服务</h1></div>
  </header>
  <div class="workspace-content">
    <div class="page">
      <p class="hint">
        {{
          canManageLinks
            ? "计算服务支持网状互联：本机既可接收其它节点连入，也可同时作为计算节点连入多个外部 Hub；本地与外部任务共用显存自动排队，下载的视频留在节点上。"
            : "转写、画面与整理由具备相应能力的计算节点完成。节点主动连接 Hub，不需要公网地址；下载的视频留在节点上。"
        }}
      </p>

      <section v-if="canManageLinks" class="mesh-strip" aria-label="网状计算概览">
        <div class="mesh-node">
          <span class="mesh-label">连入本机</span>
          <strong>{{ incomingOnlineCount }} / {{ incomingWorkers.length }} 在线</strong>
          <small class="muted">为本机工作区提供算力</small>
        </div>
        <span class="mesh-arrow" aria-hidden="true">⇄</span>
        <div class="mesh-node mesh-self">
          <span class="mesh-label">本机节点</span>
          <strong>{{ hubLinks.data.value?.default_name || "本机" }}</strong>
          <small class="muted">{{ hubLinks.data.value?.capabilities?.length ?? 0 }} 项可提供能力</small>
        </div>
        <span class="mesh-arrow" aria-hidden="true">⇄</span>
        <div class="mesh-node">
          <span class="mesh-label">连出到外部 Hub</span>
          <strong>{{ outgoingOnlineCount }} / {{ outgoingLinks.length }} 在线</strong>
          <small class="muted">向外提供转写、画面与整理</small>
        </div>
      </section>

      <section v-if="canManageLinks" class="panel">
        <div class="panel-head">
          <h3>连出到外部 Hub</h3>
          <button class="quiet-button small" @click="showAddHub = !showAddHub">
            {{ showAddHub ? "收起" : "+ 连接新 Hub" }}
          </button>
        </div>
        <p class="hint">
          检测到 <code>.env</code> 中配置了 <code>COURSE2MD_HUB_URL</code> 与
          <code>COURSE2MD_WORKER_TOKEN</code> 时，启动本地服务会自动连入；也可以在这里同时连接多个外部 Hub。
        </p>

        <div v-if="outgoingLinks.length" class="worker-list">
          <article v-for="link in outgoingLinks" :key="link.id" class="worker-card hub-link-card">
            <div class="worker-head">
              <span class="online" :class="linkDotClass(link.status)"></span>
              <a
                class="hub-link-url"
                :href="`${link.hub_url}/workers`"
                target="_blank"
                rel="noopener noreferrer"
                >{{ hubHost(link.hub_url) }}</a
              >
              <span class="pill">{{ link.source === "env" ? "来自 .env" : "外部 Hub" }}</span>
              <span class="muted">{{ linkStatusLabels[link.status ?? "disconnected"] ?? link.status }}</span>
              <div class="worker-actions">
                <button
                  v-if="link.status === 'disconnected' || link.status === 'auth_failed'"
                  class="primary-button small"
                  :disabled="busyLink === link.id"
                  @click="connectLink(link.id)"
                >
                  连接
                </button>
                <button
                  v-else
                  class="quiet-button small"
                  :disabled="busyLink === link.id"
                  @click="disconnectLink(link)"
                >
                  断开
                </button>
                <button
                  class="quiet-button small"
                  :disabled="busyLink === link.id"
                  @click="editingId === link.id ? (editingId = null) : startEdit(link)"
                >
                  {{ editingId === link.id ? "取消" : "编辑" }}
                </button>
                <button class="quiet-button small" :disabled="busyLink === link.id" @click="deleteLink(link)">
                  移除
                </button>
              </div>
            </div>

            <div v-if="link.current_task" class="link-busy">
              正在执行阶段 <strong>{{ link.current_task.stage }}</strong>（任务
              {{ link.current_task.job_id }}）
            </div>
            <div v-if="link.last_error" class="link-error">
              {{ link.last_error }}
            </div>

            <div class="capabilities">
              <span v-for="capability in link.capabilities ?? []" :key="capability" class="tag">{{
                label(capability)
              }}</span>
            </div>

            <small class="muted">
              {{ link.name }}
              {{ link.worker_id ? ` · ${link.worker_id}` : "" }}
              {{ link.token_hint ? ` · 令牌 ${link.token_hint}` : "" }}
              {{ link.last_heartbeat ? ` · 最近心跳 ${dateTime(link.last_heartbeat)}` : "" }}
              {{ link.completed_tasks ? ` · 已完成 ${link.completed_tasks} 次` : "" }}
              {{ link.auto_connect ? " · 启动自动连" : "" }}
            </small>

            <div v-if="editingId === link.id" class="hub-edit-box">
              <div class="hub-form-grid">
                <label class="field">
                  <span>Hub 地址</span>
                  <input
                    v-model="editForm.hub_url"
                    placeholder="https://course.example.com"
                    @input="handleHubInput(editForm)"
                  />
                </label>
                <label class="field">
                  <span>新令牌（留空沿用已保存）</span>
                  <input
                    v-model="editForm.token"
                    type="password"
                    placeholder="留空保持不变"
                    autocomplete="off"
                  />
                </label>
                <label class="field">
                  <span>节点名称</span>
                  <input
                    v-model="editForm.name"
                    :placeholder="hubLinks.data.value?.default_name || '我的电脑'"
                  />
                </label>
                <label v-if="session.mode === 'local'" class="field">
                  <span>工作目录</span>
                  <input
                    v-model="editForm.work_dir"
                    :placeholder="hubLinks.data.value?.default_work_dir || 'course2md-worker'"
                  />
                </label>
              </div>
              <div class="inline-form">
                <label class="check-inline">
                  <input v-model="editForm.auto_connect" type="checkbox" /> 启动时自动连接
                </label>
                <label class="check-inline">
                  <input v-model="editForm.no_asr" type="checkbox" /> 不承担转写
                </label>
                <label class="check-inline">
                  <input v-model="editForm.no_agents" type="checkbox" /> 不承担 Agent 整理
                </label>
                <button
                  class="primary-button small"
                  :disabled="busyLink === link.id"
                  @click="saveEdit(link.id)"
                >
                  保存配置
                </button>
              </div>
            </div>

            <div v-if="link.logs?.length" class="link-logs-wrap">
              <button
                type="button"
                class="text-button"
                @click="expandedLogs[link.id] = !expandedLogs[link.id]"
              >
                {{ expandedLogs[link.id] ? "收起日志" : `查看日志（${link.logs.length}）` }}
              </button>
              <pre v-if="expandedLogs[link.id]" class="link-logs">{{ link.logs.join("\n") }}</pre>
            </div>
          </article>
        </div>

        <p v-else-if="!showAddHub" class="muted">
          尚未连接到外部 Hub。点击右上角「+ 连接新 Hub」，或在 <code>.env</code> 配置后重启即可自动连入。
        </p>

        <form v-if="showAddHub || !outgoingLinks.length" class="hub-add-form" @submit.prevent="addHubLink">
          <div class="hub-form-grid">
            <label class="field">
              <span>Hub 地址（可直接粘贴整条 worker 命令）</span>
              <input
                v-model="newLink.hub_url"
                placeholder="https://course.example.com 或粘贴 course2md worker --hub ... --token ..."
                required
                @input="handleHubInput(newLink)"
              />
            </label>
            <label class="field">
              <span>节点令牌</span>
              <input
                v-model="newLink.token"
                type="password"
                placeholder="c2m_..."
                required
                autocomplete="off"
              />
            </label>
            <label class="field">
              <span>节点名称（可选）</span>
              <input v-model="newLink.name" :placeholder="hubLinks.data.value?.default_name || '我的电脑'" />
            </label>
            <label v-if="session.mode === 'local'" class="field">
              <span>工作目录（可选）</span>
              <input
                v-model="newLink.work_dir"
                :placeholder="hubLinks.data.value?.default_work_dir || 'course2md-worker'"
              />
            </label>
          </div>
          <div class="inline-form">
            <label class="check-inline">
              <input v-model="newLink.auto_connect" type="checkbox" /> 启动时自动连接
            </label>
            <label class="check-inline">
              <input v-model="newLink.no_asr" type="checkbox" /> 不承担转写
            </label>
            <label class="check-inline">
              <input v-model="newLink.no_agents" type="checkbox" /> 不承担 Agent 整理
            </label>
            <button type="submit" class="primary-button" :disabled="busyLink === 'new'">
              {{ busyLink === "new" ? "连接中…" : "添加并连接" }}
            </button>
          </div>
        </form>
      </section>

      <section class="panel">
        <h3>{{ canManageLinks ? "连入本机的节点" : "节点" }}</h3>
        <div class="worker-list">
          <article v-for="worker in incomingWorkers" :key="worker.id" class="worker-card">
            <div class="worker-head">
              <span class="online" :class="{ on: worker.online }"></span>
              <strong>{{ worker.name || worker.id }}</strong>
              <span class="pill">{{
                worker.kind === "builtin" ? "内置" : worker.owner_id ? "个人节点" : "共享节点"
              }}</span>
              <span class="muted">{{ worker.online ? "在线" : worker.disabled ? "已停用" : "离线" }}</span>
              <div v-if="canManage(worker)" class="worker-actions">
                <button
                  v-if="!worker.disabled"
                  class="quiet-button small"
                  :disabled="removing === worker.id"
                  @click="disable(worker.id, worker.name || worker.id)"
                >
                  停用
                </button>
                <button
                  class="quiet-button small"
                  :disabled="removing === worker.id"
                  @click="remove(worker.id, worker.name || worker.id)"
                >
                  {{ removing === worker.id ? "删除中…" : "删除" }}
                </button>
              </div>
            </div>
            <div class="capabilities">
              <span v-for="capability in worker.capabilities" :key="capability" class="tag">{{
                label(capability)
              }}</span>
              <span v-if="!worker.capabilities?.length" class="muted">尚未连接</span>
            </div>
            <small class="muted"
              >{{ worker.id }}{{ worker.version ? ` · ${worker.version}` : ""
              }}{{ worker.last_seen ? ` · 最近在线 ${dateTime(worker.last_seen)}` : "" }}</small
            >
          </article>
        </div>
      </section>

      <section class="panel">
        <h3>{{ canManageLinks ? "允许其它节点连入本机" : "添加节点" }}</h3>
        <div class="inline-form">
          <label class="field compact"
            ><span>节点名称</span><input v-model="name" placeholder="例如：我的电脑"
          /></label>
          <label v-if="isAdmin() && session.mode === 'server'" class="check-inline">
            <input v-model="personal" type="checkbox" /> 只处理我自己的任务
          </label>
          <button class="primary-button" @click="create">生成令牌</button>
        </div>
        <div v-if="created" class="token-box">
          <p>
            「{{ created.name }}」的令牌只显示这一次（{{
              created.shared ? "共享节点，可处理所有人的任务" : "个人节点"
            }}）：
          </p>
          <code class="copyable" @click="copy(created.token)">{{ created.token }}</code>
          <p>在另一台电脑运行，或直接复制下一行粘贴到对方网页「计算服务 → 连出到外部 Hub」：</p>
          <pre class="copyable" @click="copy(`course2md worker --hub ${hubUrl} --token ${created.token}`)">
uv tool install "course2md[asr,cuda]"
course2md worker --hub {{ hubUrl }} --token {{ created.token }}</pre>
          <p class="hint">
            没有 NVIDIA 显卡时去掉 cuda；只想承担整理任务可加 --without
            asr。局域网节点连接本地应用时，本地应用需用 --host 0.0.0.0 启动。
          </p>
        </div>
      </section>
    </div>
  </div>
  <ConfirmDialog ref="confirmation" />
</template>
