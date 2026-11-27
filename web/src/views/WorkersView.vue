<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";

import { client, type LocalNode, unwrap, type Worker } from "@/api/client";
import CapabilityTags from "@/components/CapabilityTags.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import ExternalHubsSection from "@/components/ExternalHubsSection.vue";
import LocalNodeDialog, { type LocalNodeForm } from "@/components/LocalNodeDialog.vue";
import MeshTopology, { type MeshKind, type MeshNode } from "@/components/MeshTopology.vue";
import NodeDetailDialog, { type DetailAction, type DetailTarget } from "@/components/NodeDetailDialog.vue";
import { isAdmin, session } from "@/stores/session";
import { showError, toast, ui } from "@/stores/ui";
import { dateTime } from "@/utils/format";
import { hubHost, linkMeshStatus, linkStatusLabels, localNodeStatus, workerStatus } from "@/utils/mesh";

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

const externalHubs = ref<InstanceType<typeof ExternalHubsSection> | null>(null);
const localNodeDialog = ref<InstanceType<typeof LocalNodeDialog> | null>(null);
const localNodeBusy = ref(false);
const selected = ref<{ kind: MeshKind; id: string } | null>(null);

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
    externalHubs.value?.openAdd();
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
    if (action === "connect-link") await externalHubs.value?.connect(link.id);
    else if (action === "disconnect-link") await externalHubs.value?.disconnect(link);
    else if (action === "delete-link") await externalHubs.value?.remove(link);
    else if (action === "edit-link") {
      selected.value = null;
      externalHubs.value?.edit(link);
      scrollToSection(`link-${link.id}`);
    }
  }
  // 节点被删除或停用后详情已无对应对象，关闭弹窗
  if (!detail.value) selected.value = null;
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
    <div class="page workers-page">
      <p class="hint">
        {{
          canManageLinks
            ? "本机既是接收其它节点连入的 Hub，也可以作为计算节点连入多个外部 Hub。本地与外部任务共用显存自动排队，下载的视频留在节点上。"
            : "转写、画面与整理由具备相应能力的计算节点完成。节点主动连接 Hub，不需要公网地址；下载的视频留在节点上。"
        }}
      </p>

      <section class="mesh-card" aria-label="网状计算拓扑">
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
        <div class="mesh-foot">
          <ul class="mesh-legend">
            <li class="kind-hub"><span class="swatch"></span><strong>本机 Hub</strong></li>
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
          <span class="mesh-tip">点击查看详情，可拖动 · 虚线离线，流动的线执行中</span>
        </div>
      </section>

      <section v-if="canManageLocalNodes" id="section-local-nodes" class="node-section">
        <div class="section-head">
          <h2>
            本机节点<span class="pill">{{ localNodeList.length }}</span>
          </h2>
          <button class="quiet-button small" @click="localNodeDialog?.openCreate()">+ 新建本机节点</button>
        </div>
        <p class="hint">
          与 Hub 同进程运行，可各自承担一部分能力。所有本机节点共享显存，一次只执行一个阶段。
        </p>
        <div class="worker-list">
          <article v-for="node in localNodeList" :key="node.id" class="node-card local-node-card">
            <div class="worker-head">
              <span class="node-icon kind-local" aria-hidden="true"></span>
              <div class="node-name">
                <div class="node-title">
                  <button type="button" class="node-link" @click="selected = { kind: 'local', id: node.id }">
                    {{ node.name || node.id }}
                  </button>
                  <span v-if="node.builtin" class="pill">内置</span>
                </div>
                <small class="node-meta"
                  >{{ node.auto ? "能力自动跟随本机检测" : "能力为手动选择" }} · {{ node.id }}</small
                >
              </div>
              <span class="status-chip"
                ><span class="online" :class="localNodeStatus(node)"></span
                >{{ !node.enabled ? "已停用" : node.busy ? "执行中" : node.online ? "在线" : "离线" }}</span
              >
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
            <CapabilityTags :capabilities="node.capabilities" empty="没有可承担的能力" />
          </article>
        </div>
      </section>

      <ExternalHubsSection
        v-if="canManageLinks"
        ref="externalHubs"
        :links="outgoingLinks"
        :default-name="hubLinks.data.value?.default_name"
        :confirm="confirmation"
        :default-work-dir="hubLinks.data.value?.default_work_dir"
        @changed="hubLinks.refetch()"
      />

      <section class="node-section">
        <div class="section-head">
          <h2>
            {{ canManageLinks ? "连入本机的节点" : "节点"
            }}<span class="pill">{{ incomingWorkers.length }}</span>
          </h2>
        </div>
        <p v-if="!incomingWorkers.length" class="node-empty">
          {{
            session.mode === "server"
              ? "当前服务器以纯 Hub 模式运行，尚未连接任何计算节点。请在下方生成令牌，并在装有显卡或解析插件的电脑上通过本地网页「计算服务 → 连出到外部 Hub」或 course2md worker 接入。"
              : "暂无连入的计算节点，可在下方生成令牌以允许其它电脑接入。"
          }}
        </p>
        <div v-else class="worker-list">
          <article v-for="worker in incomingWorkers" :key="worker.id" class="node-card worker-card">
            <div class="worker-head">
              <span
                class="node-icon"
                :class="worker.kind === 'builtin' ? 'kind-local' : 'kind-remote'"
                aria-hidden="true"
              ></span>
              <div class="node-name">
                <div class="node-title">
                  <strong>{{ worker.name || worker.id }}</strong>
                  <span class="pill">{{
                    worker.kind === "builtin" ? "内置" : worker.owner_id ? "个人节点" : "共享节点"
                  }}</span>
                </div>
                <small class="node-meta"
                  >{{ worker.id }}{{ worker.version ? ` · ${worker.version}` : ""
                  }}{{ worker.last_seen ? ` · 最近在线 ${dateTime(worker.last_seen)}` : "" }}</small
                >
              </div>
              <span class="status-chip"
                ><span class="online" :class="{ on: worker.online }"></span
                >{{ worker.online ? "在线" : worker.disabled ? "已停用" : "离线" }}</span
              >
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
            <CapabilityTags :capabilities="worker.capabilities" empty="尚未连接" />
          </article>
        </div>
      </section>

      <section id="section-add-node" class="node-section">
        <div class="section-head">
          <h2>{{ canManageLinks ? "允许其它节点连入本机" : "添加节点" }}</h2>
        </div>
        <div class="add-node-card">
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
        </div>
      </section>
    </div>
  </div>
  <ConfirmDialog ref="confirmation" />
  <NodeDetailDialog
    :target="detail"
    :busy="localNodeBusy || (externalHubs?.busy ?? null) !== null || removing !== null"
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
