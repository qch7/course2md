<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute } from "vue-router";

import {
  type BundleImportResponse,
  client,
  courseBundleUrl,
  importBundle,
  type Job,
  jobBundleUrl,
  type RemoteCatalogResponse,
  type TransferPeer,
  type TransferResultResponse,
  unwrap,
} from "@/api/client";
import AppIcon from "@/components/AppIcon.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import { absorb, absorbCourse, isBusy, store } from "@/stores/jobs";
import { showError, toast, ui } from "@/stores/ui";
import { agentName, sessionLabel, statusNames } from "@/utils/format";

type ConflictMode = "skip" | "overwrite" | "copy";
type TransferTab = "push" | "pull" | "bundle";

const route = useRoute();
const confirmation = ref<InstanceType<typeof ConfirmDialog> | null>(null);
const tab = ref<TransferTab>("push");

const peersQuery = useQuery({
  queryKey: ["transfer-peers"],
  queryFn: () => unwrap(client.GET("/api/v1/transfer/peers")),
});

const peers = computed(() => peersQuery.data.value?.peers ?? []);

// ------------------------------------------------------------------ 对端 Hub 管理
const showAddPeer = ref(false);
const savingPeer = ref(false);
const editingPeerId = ref<string | null>(null);
const newPeer = reactive({ name: "", hub_url: "", token: "" });
const editPeer = reactive({ name: "", hub_url: "", token: "" });

async function addPeer(): Promise<void> {
  if (!newPeer.hub_url.trim()) return showError("请先填写对端 Hub 地址");
  savingPeer.value = true;
  try {
    const data = await unwrap(
      client.POST("/api/v1/transfer/peers", {
        body: {
          name: newPeer.name.trim(),
          hub_url: newPeer.hub_url.trim(),
          token: newPeer.token.trim(),
        },
      }),
    );
    newPeer.name = "";
    newPeer.hub_url = "";
    newPeer.token = "";
    showAddPeer.value = false;
    await peersQuery.refetch();
    if (!pushTarget.peerId) pushTarget.peerId = data.peer.id;
    if (!pullSource.peerId) pullSource.peerId = data.peer.id;
    toast("已保存对端 Hub");
  } catch (error) {
    showError(error);
  } finally {
    savingPeer.value = false;
  }
}

function startEditPeer(peer: TransferPeer): void {
  editingPeerId.value = peer.id;
  editPeer.name = peer.name ?? "";
  editPeer.hub_url = peer.hub_url ?? "";
  editPeer.token = "";
}

async function saveEditPeer(peerId: string): Promise<void> {
  savingPeer.value = true;
  try {
    const body: Record<string, string> = {
      name: editPeer.name.trim(),
      hub_url: editPeer.hub_url.trim(),
    };
    if (editPeer.token.trim()) body.token = editPeer.token.trim();
    await unwrap(
      client.PATCH("/api/v1/transfer/peers/{peer_id}", {
        params: { path: { peer_id: peerId } },
        body,
      }),
    );
    editingPeerId.value = null;
    await peersQuery.refetch();
    toast("已更新对端 Hub");
  } catch (error) {
    showError(error);
  } finally {
    savingPeer.value = false;
  }
}

async function deletePeer(peer: TransferPeer): Promise<void> {
  const ok = await confirmation.value?.open({
    title: "删除对端 Hub",
    message: `确定移除「${peer.name || peer.hub_url}」？`,
    detail: "只移除当前保存的连接信息，不影响已传输的课程与笔记。",
    confirmText: "移除",
  });
  if (!ok) return;
  try {
    await unwrap(
      client.DELETE("/api/v1/transfer/peers/{peer_id}", { params: { path: { peer_id: peer.id } } }),
    );
    if (pushTarget.peerId === peer.id) pushTarget.peerId = "";
    if (pullSource.peerId === peer.id) pullSource.peerId = "";
    if (editingPeerId.value === peer.id) editingPeerId.value = null;
    await peersQuery.refetch();
    toast("已移除对端 Hub");
  } catch (error) {
    showError(error);
  }
}

function usePeerForPush(peer: TransferPeer): void {
  pushTarget.peerId = peer.id;
  tab.value = "push";
}

function usePeerForPull(peer: TransferPeer): void {
  pullSource.peerId = peer.id;
  tab.value = "pull";
  void fetchRemoteCatalog();
}

// ------------------------------------------------------------------ 本地课程分组（供推送与导出）
interface LocalGroup {
  key: string;
  courseId: string;
  title: string;
  jobs: Job[];
  transferable: Job[];
}

function isTransferable(job: Job): boolean {
  return !isBusy(job) && (job.files?.length ?? 0) > 0;
}

const localGroups = computed<LocalGroup[]>(() => {
  const map = new Map<string, LocalGroup>();
  [...store.courses.values()]
    .sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0))
    .forEach((course) =>
      map.set(course.id, {
        key: course.id,
        courseId: course.id,
        title: course.title || course.course || "课程",
        jobs: [],
        transferable: [],
      }),
    );
  [...store.jobs.values()]
    .sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
    .forEach((job) => {
      const key =
        job.course_id && map.has(job.course_id) ? job.course_id : `standalone:${job.course || "未分类"}`;
      if (!map.has(key)) {
        map.set(key, {
          key,
          courseId: "",
          title: job.course || "未分类",
          jobs: [],
          transferable: [],
        });
      }
      const group = map.get(key)!;
      group.jobs.push(job);
      if (isTransferable(job)) group.transferable.push(job);
    });
  return [...map.values()].filter((group) => group.jobs.length > 0);
});

// ------------------------------------------------------------------ 推送
const pushTarget = reactive({
  peerId: "",
  hubUrl: "",
  token: "",
  conflict: "skip" as ConflictMode,
  includeCache: false,
});
const selectedPushJobs = ref(new Set<string>());
const pushing = ref(false);
const lastResult = ref<(TransferResultResponse | BundleImportResponse) | null>(null);

function togglePushJob(jobId: string): void {
  const next = new Set(selectedPushJobs.value);
  if (next.has(jobId)) next.delete(jobId);
  else next.add(jobId);
  selectedPushJobs.value = next;
}

function togglePushGroup(group: LocalGroup): void {
  const next = new Set(selectedPushJobs.value);
  const allSelected = group.transferable.length > 0 && group.transferable.every((item) => next.has(item.id));
  for (const job of group.transferable) {
    if (allSelected) next.delete(job.id);
    else next.add(job.id);
  }
  selectedPushJobs.value = next;
}

function groupAllSelected(group: LocalGroup): boolean {
  return (
    group.transferable.length > 0 && group.transferable.every((item) => selectedPushJobs.value.has(item.id))
  );
}

async function runPush(): Promise<void> {
  const jobIds = [...selectedPushJobs.value];
  if (!jobIds.length) return showError("请先勾选要推送的课次");
  if (!pushTarget.peerId && !pushTarget.hubUrl.trim()) return showError("请选择或填写目标 Hub 地址");
  pushing.value = true;
  lastResult.value = null;
  try {
    const data = await unwrap(
      client.POST("/api/v1/transfer/push", {
        body: {
          peer_id: pushTarget.peerId || null,
          hub_url: pushTarget.peerId ? null : pushTarget.hubUrl.trim(),
          token: pushTarget.peerId ? null : pushTarget.token.trim(),
          job_ids: jobIds,
          conflict: pushTarget.conflict,
          include_cache: pushTarget.includeCache,
        },
      }),
    );
    lastResult.value = data;
    toast(summaryText(data));
  } catch (error) {
    showError(error);
  } finally {
    pushing.value = false;
  }
}

// ------------------------------------------------------------------ 拉取
const pullSource = reactive({
  peerId: "",
  hubUrl: "",
  token: "",
  conflict: "skip" as ConflictMode,
  includeCache: false,
});
const loadingRemote = ref(false);
const pulling = ref(false);
const remoteCatalog = ref<RemoteCatalogResponse | null>(null);
const selectedPullJobs = ref(new Set<string>());

async function fetchRemoteCatalog(): Promise<void> {
  if (!pullSource.peerId && !pullSource.hubUrl.trim()) return showError("请选择或填写来源 Hub 地址");
  loadingRemote.value = true;
  remoteCatalog.value = null;
  selectedPullJobs.value = new Set();
  try {
    const data = await unwrap(
      client.POST("/api/v1/transfer/remote", {
        body: {
          peer_id: pullSource.peerId || null,
          hub_url: pullSource.peerId ? null : pullSource.hubUrl.trim(),
          token: pullSource.peerId ? null : pullSource.token.trim(),
        },
      }),
    );
    remoteCatalog.value = data;
    const initial = new Set<string>();
    for (const course of data.courses ?? []) {
      for (const job of course.jobs ?? []) {
        if (job.transferable) initial.add(job.id);
      }
    }
    selectedPullJobs.value = initial;
    if (!(data.courses ?? []).length) toast("对端 Hub 暂无可拉取的课程产物");
  } catch (error) {
    showError(error);
  } finally {
    loadingRemote.value = false;
  }
}

function togglePullJob(jobId: string): void {
  const next = new Set(selectedPullJobs.value);
  if (next.has(jobId)) next.delete(jobId);
  else next.add(jobId);
  selectedPullJobs.value = next;
}

async function runPull(): Promise<void> {
  const jobIds = [...selectedPullJobs.value];
  if (!jobIds.length) return showError("请先勾选要拉取的对端课次");
  pulling.value = true;
  lastResult.value = null;
  try {
    const data = await unwrap(
      client.POST("/api/v1/transfer/pull", {
        body: {
          peer_id: pullSource.peerId || null,
          hub_url: pullSource.peerId ? null : pullSource.hubUrl.trim(),
          token: pullSource.peerId ? null : pullSource.token.trim(),
          job_ids: jobIds,
          conflict: pullSource.conflict,
          include_cache: pullSource.includeCache,
        },
      }),
    );
    absorbTransferResult(data);
    lastResult.value = data;
    toast(summaryText(data));
  } catch (error) {
    showError(error);
  } finally {
    pulling.value = false;
  }
}

// ------------------------------------------------------------------ 离线产物包导入
const exportWithCache = ref(false);
const bundleFile = ref<File | null>(null);
const bundleConflict = ref<ConflictMode>("skip");
const importingBundle = ref(false);
const fileInput = ref<HTMLInputElement | null>(null);

function onFileChange(event: Event): void {
  const target = event.target as HTMLInputElement;
  bundleFile.value = target.files?.[0] ?? null;
}

async function uploadBundle(): Promise<void> {
  if (!bundleFile.value) return showError("请先选择 .c2m.zip 产物包");
  importingBundle.value = true;
  lastResult.value = null;
  try {
    const data = await importBundle(bundleFile.value, bundleConflict.value);
    absorbBundleResult(data);
    lastResult.value = data;
    bundleFile.value = null;
    if (fileInput.value) fileInput.value.value = "";
    toast(summaryText(data));
  } catch (error) {
    showError(error);
  } finally {
    importingBundle.value = false;
  }
}

// ------------------------------------------------------------------ 辅助
function absorbTransferResult(data: TransferResultResponse): void {
  for (const course of data.courses ?? []) absorbCourse(course);
  for (const job of [...(data.imported ?? []), ...(data.overwritten ?? [])]) absorb(job);
}

function absorbBundleResult(data: BundleImportResponse): void {
  if (data.course) absorbCourse(data.course);
  for (const job of [...(data.imported ?? []), ...(data.overwritten ?? [])]) absorb(job);
}

function summaryText(data: TransferResultResponse | BundleImportResponse): string {
  const imp = data.imported?.length ?? 0;
  const ov = data.overwritten?.length ?? 0;
  const sk = data.skipped?.length ?? 0;
  return `传输完成：新增 ${imp} 节，覆盖 ${ov} 节，跳过 ${sk} 节`;
}

let appliedRouteKey = "";

function applyRouteSelection(): void {
  const jobId = typeof route.query.jobId === "string" ? route.query.jobId : "";
  const courseId = typeof route.query.courseId === "string" ? route.query.courseId : "";
  const key = `${jobId}|${courseId}`;
  if (!jobId && !courseId) return;
  if (appliedRouteKey === key && selectedPushJobs.value.size > 0) return;
  if (jobId) {
    const job = store.jobs.get(jobId);
    if (job && isTransferable(job)) {
      selectedPushJobs.value = new Set([jobId]);
      tab.value = "push";
      appliedRouteKey = key;
    }
  } else if (courseId) {
    const group = localGroups.value.find((item) => item.courseId === courseId);
    if (group && group.transferable.length) {
      selectedPushJobs.value = new Set(group.transferable.map((item) => item.id));
      tab.value = "push";
      appliedRouteKey = key;
    }
  }
}

watch(
  peers,
  (list) => {
    if (list.length) {
      if (!pushTarget.peerId && !pushTarget.hubUrl) pushTarget.peerId = list[0]!.id;
      if (!pullSource.peerId && !pullSource.hubUrl) pullSource.peerId = list[0]!.id;
    }
  },
  { immediate: true },
);

watch(
  () => [route.query.jobId, route.query.courseId, store.jobs.size, store.courses.size],
  () => applyRouteSelection(),
);

onMounted(() => {
  applyRouteSelection();
});
</script>

<template>
  <header class="topbar">
    <button
      class="icon-button mobile-menu"
      aria-label="展开课程列表"
      @click="ui.sidebarOpen = !ui.sidebarOpen"
    >
      <AppIcon name="menu" :size="19" />
    </button>
    <div class="course-heading"><h1>课程互传</h1></div>
  </header>
  <div class="workspace-content">
    <div class="page">
      <!-- 对端 Hub 管理 -->
      <section class="panel">
        <div class="panel-header">
          <div>
            <h3>常用对端 Hub</h3>
            <p class="hint">
              在多个本地应用或服务器 Hub
              间互传课程笔记、转写、课件截图、对话上下文与版本历史；传输时自动剥离登录凭证与原始视频。
            </p>
          </div>
          <button class="primary-button" @click="showAddPeer = !showAddPeer">
            {{ showAddPeer ? "收起" : "添加对端 Hub" }}
          </button>
        </div>

        <form v-if="showAddPeer" class="hub-add-form" @submit.prevent="addPeer">
          <div class="hub-form-grid">
            <label class="field compact">
              <span>对端 Hub 地址</span>
              <input v-model="newPeer.hub_url" placeholder="例如 https://course.example.com" required />
            </label>
            <label class="field compact">
              <span>名称（可选）</span>
              <input v-model="newPeer.name" placeholder="例如 实验室服务器 / 我的台式机" />
            </label>
          </div>
          <label class="field compact">
            <span>API 令牌（对端为服务器形态时必填，在对端「账号」页生成；本地应用可留空）</span>
            <input
              v-model="newPeer.token"
              type="password"
              autocomplete="off"
              placeholder="留空或粘贴 API 令牌"
            />
          </label>
          <div class="inline-actions">
            <button type="submit" class="primary-button" :disabled="savingPeer">保存对端</button>
            <button type="button" class="quiet-button" @click="showAddPeer = false">取消</button>
          </div>
        </form>

        <div v-if="peers.length" class="transfer-peers">
          <div v-for="peer in peers" :key="peer.id" class="transfer-peer-card">
            <template v-if="editingPeerId === peer.id">
              <div class="hub-edit-box">
                <div class="hub-form-grid">
                  <label class="field compact">
                    <span>名称</span>
                    <input v-model="editPeer.name" />
                  </label>
                  <label class="field compact">
                    <span>Hub 地址</span>
                    <input v-model="editPeer.hub_url" />
                  </label>
                </div>
                <label class="field compact">
                  <span>新 API 令牌（留空则沿用已保存令牌）</span>
                  <input v-model="editPeer.token" type="password" autocomplete="off" />
                </label>
                <div class="inline-actions">
                  <button class="primary-button" :disabled="savingPeer" @click="saveEditPeer(peer.id)">
                    保存
                  </button>
                  <button class="quiet-button" @click="editingPeerId = null">取消</button>
                </div>
              </div>
            </template>
            <template v-else>
              <div class="peer-main">
                <strong>{{ peer.name || peer.hub_url }}</strong>
                <span class="pill">{{ peer.has_token ? `令牌 ${peer.token_hint}` : "无令牌" }}</span>
                <small class="peer-url">{{ peer.hub_url }}</small>
              </div>
              <div class="peer-actions">
                <button class="quiet-button" @click="usePeerForPush(peer)">推送到此 Hub</button>
                <button class="quiet-button" @click="usePeerForPull(peer)">从此 Hub 拉取</button>
                <button class="quiet-button" @click="startEditPeer(peer)">编辑</button>
                <button class="quiet-button danger" @click="deletePeer(peer)">删除</button>
              </div>
            </template>
          </div>
        </div>
        <p v-else-if="!showAddPeer" class="hint">
          尚未保存对端 Hub，可直接添加常用 Hub，或在下方临时填写地址。
        </p>
      </section>

      <!-- 操作区 -->
      <section class="panel">
        <div class="view-tabs" role="tablist" aria-label="互传方式">
          <button role="tab" :aria-selected="tab === 'push'" @click="tab = 'push'">推送到对端 Hub</button>
          <button role="tab" :aria-selected="tab === 'pull'" @click="tab = 'pull'">从对端 Hub 拉取</button>
          <button role="tab" :aria-selected="tab === 'bundle'" @click="tab = 'bundle'">
            离线产物包（.c2m.zip）
          </button>
        </div>

        <!-- 推送 -->
        <div v-if="tab === 'push'" class="transfer-pane">
          <div class="hub-form-grid">
            <label class="field compact">
              <span>目标 Hub</span>
              <select v-model="pushTarget.peerId">
                <option value="">临时填写地址</option>
                <option v-for="peer in peers" :key="peer.id" :value="peer.id">
                  {{ peer.name }}（{{ peer.hub_url }}）
                </option>
              </select>
            </label>
            <label class="field compact">
              <span>目标已有同名课次时</span>
              <select v-model="pushTarget.conflict">
                <option value="skip">跳过已有课次</option>
                <option value="overwrite">覆盖目标课次</option>
                <option value="copy">另存为新副本</option>
              </select>
            </label>
          </div>
          <label class="check-line">
            <input v-model="pushTarget.includeCache" type="checkbox" />
            <span>带上各阶段缓存</span>
          </label>
          <p class="hint">
            对端可直接从整理等中间阶段重新运行（例如换 Agent 重新整理），不必重新下载与转写；包会稍大一些。
          </p>
          <div v-if="!pushTarget.peerId" class="hub-form-grid">
            <label class="field compact">
              <span>目标 Hub 地址</span>
              <input v-model="pushTarget.hubUrl" placeholder="例如 https://course.example.com" />
            </label>
            <label class="field compact">
              <span>目标 API 令牌（服务器形态必填）</span>
              <input v-model="pushTarget.token" type="password" autocomplete="off" />
            </label>
          </div>

          <div v-if="localGroups.length" class="transfer-tree">
            <div v-for="group in localGroups" :key="group.key" class="transfer-course-box">
              <label class="transfer-course-head">
                <input
                  type="checkbox"
                  :checked="groupAllSelected(group)"
                  :disabled="!group.transferable.length"
                  @change="togglePushGroup(group)"
                />
                <strong>{{ group.title }}</strong>
                <span class="pill">{{ group.transferable.length }} / {{ group.jobs.length }} 节可传</span>
              </label>
              <div class="transfer-job-list">
                <label
                  v-for="job in group.jobs"
                  :key="job.id"
                  class="transfer-job-row"
                  :class="{ disabled: !isTransferable(job) }"
                >
                  <input
                    type="checkbox"
                    :checked="selectedPushJobs.has(job.id)"
                    :disabled="!isTransferable(job)"
                    @change="togglePushJob(job.id)"
                  />
                  <span class="transfer-job-title">{{ sessionLabel(job) }}</span>
                  <small>
                    {{ agentName(job.agent) }} · {{ statusNames[job.status ?? ""] ?? job.status }} ·
                    {{ job.files?.length ?? 0 }} 个文件
                  </small>
                </label>
              </div>
            </div>
          </div>
          <p v-else class="hint">当前 Hub 还没有可推送的课程产物。</p>

          <div class="inline-actions">
            <button class="primary-button" :disabled="pushing || !selectedPushJobs.size" @click="runPush">
              {{ pushing ? "正在打包并推送…" : `推送所选（${selectedPushJobs.size} 节）` }}
            </button>
          </div>
        </div>

        <!-- 拉取 -->
        <div v-if="tab === 'pull'" class="transfer-pane">
          <div class="hub-form-grid">
            <label class="field compact">
              <span>来源 Hub</span>
              <select v-model="pullSource.peerId">
                <option value="">临时填写地址</option>
                <option v-for="peer in peers" :key="peer.id" :value="peer.id">
                  {{ peer.name }}（{{ peer.hub_url }}）
                </option>
              </select>
            </label>
            <label class="field compact">
              <span>本机已有同名课次时</span>
              <select v-model="pullSource.conflict">
                <option value="skip">跳过已有课次</option>
                <option value="overwrite">覆盖本机课次</option>
                <option value="copy">另存为新副本</option>
              </select>
            </label>
          </div>
          <label class="check-line">
            <input v-model="pullSource.includeCache" type="checkbox" />
            <span>带回各阶段缓存</span>
          </label>
          <p class="hint">拉取后可在本机直接从整理等中间阶段重新运行，不必重新下载与转写。</p>
          <div v-if="!pullSource.peerId" class="hub-form-grid">
            <label class="field compact">
              <span>来源 Hub 地址</span>
              <input v-model="pullSource.hubUrl" placeholder="例如 https://course.example.com" />
            </label>
            <label class="field compact">
              <span>来源 API 令牌（服务器形态必填）</span>
              <input v-model="pullSource.token" type="password" autocomplete="off" />
            </label>
          </div>

          <div class="inline-actions">
            <button class="quiet-button" :disabled="loadingRemote" @click="fetchRemoteCatalog">
              {{ loadingRemote ? "正在连接对端 Hub…" : "连接并读取对端课程" }}
            </button>
            <button
              v-if="remoteCatalog?.courses?.length"
              class="primary-button"
              :disabled="pulling || !selectedPullJobs.size"
              @click="runPull"
            >
              {{ pulling ? "正在拉取并导入…" : `拉取所选（${selectedPullJobs.size} 节）` }}
            </button>
          </div>

          <div v-if="remoteCatalog" class="transfer-tree">
            <p class="hint">
              已连接 {{ remoteCatalog.hub_url }}（{{
                remoteCatalog.mode === "server" ? "服务器 Hub" : "本地 Hub"
              }}），共 {{ remoteCatalog.courses?.length ?? 0 }} 门可传输课程。
            </p>
            <div
              v-for="course in remoteCatalog.courses"
              :key="course.id || course.title"
              class="transfer-course-box"
            >
              <div class="transfer-course-head">
                <strong>{{ course.title }}</strong>
                <span class="pill">{{ course.jobs?.length ?? 0 }} 节</span>
              </div>
              <div class="transfer-job-list">
                <label
                  v-for="job in course.jobs"
                  :key="job.id"
                  class="transfer-job-row"
                  :class="{ disabled: !job.transferable }"
                >
                  <input
                    type="checkbox"
                    :checked="selectedPullJobs.has(job.id)"
                    :disabled="!job.transferable"
                    @change="togglePullJob(job.id)"
                  />
                  <span class="transfer-job-title">{{ job.title }}</span>
                  <small>
                    {{ job.session_date || "" }} · {{ statusNames[job.status ?? ""] ?? job.status }} ·
                    {{ job.file_count ?? 0 }} 个文件
                  </small>
                </label>
              </div>
            </div>
          </div>
        </div>

        <!-- 离线产物包 -->
        <div v-if="tab === 'bundle'" class="transfer-pane">
          <div class="bundle-import-box">
            <h4>导入 .c2m.zip 产物包</h4>
            <p class="hint">两个 Hub 无法直接互通时，可先导出 .c2m.zip 文件，再在此导入。</p>
            <div class="hub-form-grid">
              <label class="field compact">
                <span>选择产物包</span>
                <input ref="fileInput" type="file" accept=".zip,.c2m.zip" @change="onFileChange" />
              </label>
              <label class="field compact">
                <span>已有同名课次时</span>
                <select v-model="bundleConflict">
                  <option value="skip">跳过已有课次</option>
                  <option value="overwrite">覆盖已有课次</option>
                  <option value="copy">另存为新副本</option>
                </select>
              </label>
            </div>
            <div class="inline-actions">
              <button class="primary-button" :disabled="importingBundle || !bundleFile" @click="uploadBundle">
                {{ importingBundle ? "正在导入…" : "导入到当前 Hub" }}
              </button>
            </div>
          </div>

          <div class="bundle-export-box">
            <h4>导出当前 Hub 的课程产物包</h4>
            <label class="check-line">
              <input v-model="exportWithCache" type="checkbox" />
              <span>带上各阶段缓存</span>
            </label>
            <div v-if="localGroups.length" class="transfer-tree">
              <div v-for="group in localGroups" :key="group.key" class="transfer-course-box">
                <div class="transfer-course-head">
                  <strong>{{ group.title }}</strong>
                  <a
                    v-if="group.courseId && group.transferable.length"
                    class="quiet-button"
                    :href="courseBundleUrl(group.courseId, undefined, exportWithCache)"
                  >
                    导出整门课程（{{ group.transferable.length }} 节）
                  </a>
                </div>
                <div class="transfer-job-list">
                  <div v-for="job in group.transferable" :key="job.id" class="transfer-job-row">
                    <span class="transfer-job-title">{{ sessionLabel(job) }}</span>
                    <a class="quiet-button" :href="jobBundleUrl(job.id, exportWithCache)">导出单节</a>
                  </div>
                </div>
              </div>
            </div>
            <p v-else class="hint">暂无可导出的已完成课次。</p>
          </div>
        </div>

        <!-- 传输结果摘要 -->
        <div v-if="lastResult" class="transfer-summary">
          <strong>{{ summaryText(lastResult) }}</strong>
          <ul v-if="lastResult.skipped?.length">
            <li v-for="(item, idx) in lastResult.skipped" :key="idx">
              已跳过「{{ item.title }}」：{{ item.reason }}
            </li>
          </ul>
        </div>
      </section>
    </div>
  </div>
  <ConfirmDialog ref="confirmation" />
</template>
