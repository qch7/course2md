<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, reactive, ref } from "vue";

import { client, type HubLink, type LocalNode, unwrap, type Worker } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import LocalNodeDialog, { type LocalNodeForm } from "@/components/LocalNodeDialog.vue";
import MeshTopology, { type MeshKind, type MeshNode, type MeshStatus } from "@/components/MeshTopology.vue";
import NodeDetailDialog, { type DetailAction, type DetailTarget } from "@/components/NodeDetailDialog.vue";
import { isAdmin, session } from "@/stores/session";
import { showError, toast, ui } from "@/stores/ui";
import { capabilityLabel as label } from "@/utils/capabilities";
import { dateTime } from "@/utils/format";

const workers = useQuery({
  queryKey: ["workers"],
  queryFn: () => unwrap(client.GET("/api/v1/workers")),
  refetchInterval: 10000,
});
const localNodes = useQuery({
  queryKey: ["local-nodes"],
  queryFn: () => unwrap(client.GET("/api/v1/local-nodes")),
  enabled: computed(() => session.mode === "local" || isAdmin()),
  refetchInterval: 5000,
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
const allWorkers = computed(() => workers.data.value?.workers ?? []);
// 本机节点由 /local-nodes 管理；拿不到时（普通用户、纯 Hub）退回只读展示进程内节点
const canManageLocalNodes = computed(() => Boolean(localNodes.data.value?.can_manage));
const localNodeList = computed<LocalNode[]>(() => localNodes.data.value?.nodes ?? []);
const availableCapabilities = computed(() => localNodes.data.value?.available_capabilities ?? []);
const incomingWorkers = computed(() =>
  canManageLocalNodes.value ? allWorkers.value.filter((item) => item.kind !== "builtin") : allWorkers.value,
);
const outgoingOnlineCount = computed(
  () => outgoingLinks.value.filter((item) => item.status === "online" || item.status === "busy").length,
);

const localNodeDialog = ref<InstanceType<typeof LocalNodeDialog> | null>(null);
const localNodeBusy = ref(false);
const selected = ref<{ kind: MeshKind; id: string } | null>(null);

function workerStatus(worker: Worker): MeshStatus {
  return worker.online ? "on" : "off";
}

function localNodeStatus(node: LocalNode): MeshStatus {
  if (!node.enabled || !node.online) return "off";
  return node.busy ? "busy" : "on";
}

function linkMeshStatus(status?: string): MeshStatus {
  if (status === "online") return "on";
  if (status === "busy") return "busy";
  if (status === "connecting" || status === "reconnecting") return "warn";
  if (status === "auth_failed") return "err";
  return "off";
}

const meshLocal = computed<MeshNode[]>(() => {
  if (canManageLocalNodes.value) {
    return localNodeList.value.map((node) => ({
      id: node.id,
      kind: "local",
      label: node.name || node.id,
      sub: node.enabled ? `${node.capabilities?.length ?? 0} 项能力` : "已停用",
      status: localNodeStatus(node),
    }));
  }
  return allWorkers.value
    .filter((worker) => worker.kind === "builtin")
    .map((worker) => ({
      id: worker.id,
      kind: "local",
      label: worker.name || worker.id,
      sub: `${worker.capabilities?.length ?? 0} 项能力`,
      status: workerStatus(worker),
    }));
});
const meshRemote = computed<MeshNode[]>(() =>
  allWorkers.value
    .filter((worker) => worker.kind !== "builtin")
    .map((worker) => ({
      id: worker.id,
      kind: "remote",
      label: worker.name || worker.id,
      sub: worker.disabled ? "已停用" : `${worker.capabilities?.length ?? 0} 项能力`,
      status: workerStatus(worker),
    })),
);
const meshExternal = computed<MeshNode[]>(() =>
  outgoingLinks.value.map((link) => ({
    id: link.id,
    kind: "external",
    label: hubHost(link.hub_url),
    sub: linkStatusLabels[link.status ?? "disconnected"] ?? link.status,
    status: linkMeshStatus(link.status),
  })),
);
const meshHub = computed<MeshNode>(() => ({
  id: "hub",
  kind: "hub",
  label: "本机 Hub",
  sub: hubHost(hubUrl.value),
  status: "on",
}));
const meshAddable = computed(() => ({
  ...(canManageLocalNodes.value ? { local: "新建节点" } : {}),
  ...(canManageLinks.value ? { external: "连接 Hub" } : {}),
  remote: "生成令牌",
}));
const meshCounts = computed(() => ({
  local: {
    online: meshLocal.value.filter((item) => item.status === "on" || item.status === "busy").length,
    total: meshLocal.value.length,
  },
  remote: {
    online: meshRemote.value.filter((item) => item.status === "on" || item.status === "busy").length,
    total: meshRemote.value.length,
  },
  external: { online: outgoingOnlineCount.value, total: outgoingLinks.value.length },
}));
const selectedKey = computed(() => (selected.value ? `${selected.value.kind}:${selected.value.id}` : ""));

const detail = computed<DetailTarget | null>(() => {
  const current = selected.value;
  if (!current) return null;
  if (current.kind === "hub") {
    return {
      kind: "hub",
      name: "本机 Hub",
      address: hubUrl.value,
      mode: session.mode === "server" ? "服务器" : "本地应用",
      capabilities: canManageLocalNodes.value
        ? availableCapabilities.value
        : (hubLinks.data.value?.capabilities ?? []),
      localOnline: meshCounts.value.local.online,
      localTotal: meshCounts.value.local.total,
      remoteOnline: meshCounts.value.remote.online,
      remoteTotal: meshCounts.value.remote.total,
      externalOnline: meshCounts.value.external.online,
      externalTotal: meshCounts.value.external.total,
    };
  }
  if (current.kind === "local") {
    const node = localNodeList.value.find((item) => item.id === current.id);
    if (node) return { kind: "local", node, canManage: canManageLocalNodes.value };
    const worker = allWorkers.value.find((item) => item.id === current.id);
    if (!worker) return null;
    return {
      kind: "local",
      node: {
        id: worker.id,
        name: worker.name,
        builtin: true,
        enabled: true,
        auto: true,
        selected: [],
        capabilities: worker.capabilities,
        online: worker.online,
        busy: false,
        created_at: 0,
      },
      canManage: false,
    };
  }
  if (current.kind === "remote") {
    const worker = allWorkers.value.find((item) => item.id === current.id);
    return worker ? { kind: "remote", worker, canManage: canManage(worker) } : null;
  }
  const link = outgoingLinks.value.find((item) => item.id === current.id);
  return link ? { kind: "external", link, canManage: canManageLinks.value } : null;
});

function openNode(node: MeshNode): void {
  selected.value = { kind: node.kind, id: node.id };
}

function scrollToSection(id: string): void {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function addFromGraph(kind: Exclude<MeshKind, "hub">): void {
  if (kind === "local") {
    localNodeDialog.value?.openCreate();
  } else if (kind === "external") {
    showAddHub.value = true;
    scrollToSection("section-external-hubs");
  } else {
    scrollToSection("section-add-node");
  }
}

async function saveLocalNode(id: string | null, form: LocalNodeForm): Promise<void> {
  localNodeBusy.value = true;
  try {
    const body = {
      name: form.name.trim(),
      auto: form.auto,
      capabilities: form.auto ? [] : form.capabilities,
      enabled: form.enabled,
    };
    if (id) {
      await unwrap(
        client.PATCH("/api/v1/local-nodes/{node_id}", { params: { path: { node_id: id } }, body }),
      );
      toast("本机节点已更新");
    } else {
      await unwrap(client.POST("/api/v1/local-nodes", { body }));
      toast("本机节点已创建");
    }
    localNodeDialog.value?.close();
    await Promise.all([localNodes.refetch(), workers.refetch()]);
  } catch (error) {
    showError(error);
  } finally {
    localNodeBusy.value = false;
  }
}

async function toggleLocalNode(node: LocalNode): Promise<void> {
  localNodeBusy.value = true;
  try {
    await unwrap(
      client.PATCH("/api/v1/local-nodes/{node_id}", {
        params: { path: { node_id: node.id } },
        body: { enabled: !node.enabled },
      }),
    );
    toast(node.enabled ? "本机节点已停用" : "本机节点已启用");
    await Promise.all([localNodes.refetch(), workers.refetch()]);
  } catch (error) {
    showError(error);
  } finally {
    localNodeBusy.value = false;
  }
}

async function deleteLocalNode(node: LocalNode): Promise<void> {
  const confirmed = await confirmation.value?.open({
    title: "删除本机节点",
    message: `确定删除本机节点「${node.name || node.id}」？`,
    detail: "节点配置会被移除；课程、笔记和历史记录会保留。内置节点不能删除。",
    confirmText: "删除节点",
  });
  if (!confirmed) return;
  localNodeBusy.value = true;
  try {
    await unwrap(client.DELETE("/api/v1/local-nodes/{node_id}", { params: { path: { node_id: node.id } } }));
    if (selected.value?.id === node.id) selected.value = null;
    toast("本机节点已删除");
    await Promise.all([localNodes.refetch(), workers.refetch()]);
  } catch (error) {
    showError(error);
  } finally {
    localNodeBusy.value = false;
  }
}

async function handleDetailAction(action: DetailAction): Promise<void> {
  const target = detail.value;
  if (action === "add-local") {
    selected.value = null;
    localNodeDialog.value?.openCreate();
    return;
  }
  if (!target) return;
  if (target.kind === "local") {
    if (action === "edit-local") {
      selected.value = null;
      localNodeDialog.value?.openEdit(target.node);
    } else if (action === "toggle-local") await toggleLocalNode(target.node);
    else if (action === "delete-local") await deleteLocalNode(target.node);
  } else if (target.kind === "remote") {
    const name = target.worker.name || target.worker.id;
    if (action === "disable-worker") await disable(target.worker.id, name);
    else if (action === "delete-worker") await remove(target.worker.id, name);
  } else if (target.kind === "external") {
    const link = target.link;
    if (action === "connect-link") await connectLink(link.id);
    else if (action === "disconnect-link") await disconnectLink(link);
    else if (action === "delete-link") await deleteLink(link);
    else if (action === "edit-link") {
      selected.value = null;
      startEdit(link);
      scrollToSection(`link-${link.id}`);
    }
  }
  // 节点被删除或停用后详情已无对应对象，关闭弹窗
  if (!detail.value) selected.value = null;
}

const linkStatusLabels: Record<string, string> = {
  online: "在线",
  busy: "执行中",
  connecting: "连接中…",
  reconnecting: "重连中…",
  auth_failed: "令牌失效",
  disconnected: "未连接",
};

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

      <section class="panel mesh-panel" aria-label="网状计算拓扑">
        <MeshTopology
          :hub="meshHub"
          :local="meshLocal"
          :remote="meshRemote"
          :external="meshExternal"
          :addable="meshAddable"
          :selected-key="selectedKey"
          @select="openNode"
          @add="addFromGraph"
        />
        <ul class="mesh-legend">
          <li class="kind-hub">
            <span class="swatch"></span><strong>本机 Hub</strong><small>调度中心</small>
          </li>
          <li class="kind-local">
            <span class="swatch"></span><strong>本机节点</strong
            ><small>{{ meshCounts.local.online }} / {{ meshCounts.local.total }} 在线</small>
          </li>
          <li class="kind-remote">
            <span class="swatch"></span><strong>连入的节点</strong
            ><small>{{ meshCounts.remote.online }} / {{ meshCounts.remote.total }} 在线</small>
          </li>
          <li v-if="canManageLinks" class="kind-external">
            <span class="swatch"></span><strong>外部 Hub</strong
            ><small>{{ meshCounts.external.online }} / {{ meshCounts.external.total }} 在线</small>
          </li>
        </ul>
        <p class="hint">点击图中的 Hub 或节点查看详情与能力；虚线表示离线，流动的线表示正在执行任务。</p>
      </section>

      <section v-if="canManageLocalNodes" id="section-local-nodes" class="panel">
        <div class="panel-head">
          <h3>本机节点</h3>
          <button class="quiet-button small" @click="localNodeDialog?.openCreate()">+ 新建本机节点</button>
        </div>
        <p class="hint">
          本机节点和 Hub
          同进程运行，各自声明承担的能力（例如一个只做转写，一个只做整理）。所有本机节点与外部连接共享显存，一次只执行一个阶段。
        </p>
        <div class="worker-list">
          <article v-for="node in localNodeList" :key="node.id" class="local-node-card">
            <div class="worker-head">
              <span class="online" :class="localNodeStatus(node)"></span>
              <button type="button" class="node-link" @click="selected = { kind: 'local', id: node.id }">
                {{ node.name || node.id }}
              </button>
              <span v-if="node.builtin" class="pill">内置</span>
              <span class="muted">{{
                !node.enabled ? "已停用" : node.busy ? "执行中" : node.online ? "在线" : "离线"
              }}</span>
              <div class="worker-actions">
                <button
                  class="quiet-button small"
                  :disabled="localNodeBusy"
                  @click="localNodeDialog?.openEdit(node)"
                >
                  编辑
                </button>
                <button class="quiet-button small" :disabled="localNodeBusy" @click="toggleLocalNode(node)">
                  {{ node.enabled ? "停用" : "启用" }}
                </button>
                <button
                  v-if="!node.builtin"
                  class="quiet-button small"
                  :disabled="localNodeBusy"
                  @click="deleteLocalNode(node)"
                >
                  删除
                </button>
              </div>
            </div>
            <div class="capabilities">
              <span v-for="capability in node.capabilities" :key="capability" class="tag">{{
                label(capability)
              }}</span>
              <span v-if="!node.capabilities?.length" class="muted">没有可承担的能力</span>
            </div>
            <small class="muted"
              >{{ node.auto ? "能力自动跟随本机检测" : "能力为手动选择" }} · {{ node.id }}</small
            >
          </article>
        </div>
      </section>

      <section v-if="canManageLinks" id="section-external-hubs" class="panel">
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
          <article
            v-for="link in outgoingLinks"
            :id="`link-${link.id}`"
            :key="link.id"
            class="worker-card hub-link-card"
          >
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
        <p v-if="!incomingWorkers.length" class="muted empty-hint">
          {{
            session.mode === "server"
              ? "当前服务器以纯 Hub 模式运行，尚未连接任何计算节点。请在下方生成令牌，并在装有显卡或解析插件的电脑上通过本地网页「计算服务 → 连出到外部 Hub」或 course2md worker 接入。"
              : "暂无连入的计算节点，可在下方生成令牌以允许其它电脑接入。"
          }}
        </p>
        <div v-else class="worker-list">
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

      <section id="section-add-node" class="panel">
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
  <NodeDetailDialog
    :target="detail"
    :busy="localNodeBusy || busyLink !== null || removing !== null"
    @close="selected = null"
    @action="handleDetailAction"
  />
  <LocalNodeDialog
    ref="localNodeDialog"
    :available="availableCapabilities"
    :busy="localNodeBusy"
    @save="saveLocalNode"
  />
</template>
