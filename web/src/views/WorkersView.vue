<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";

import { client, unwrap, type Worker } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import { isAdmin, session } from "@/stores/session";
import { showError, toast, ui } from "@/stores/ui";
import { dateTime } from "@/utils/format";

const workers = useQuery({
  queryKey: ["workers"],
  queryFn: () => unwrap(client.GET("/api/v1/workers")),
  refetchInterval: 10000,
});
const name = ref("");
const personal = ref(false);
const removing = ref<string | null>(null);
const confirmation = ref<InstanceType<typeof ConfirmDialog> | null>(null);
const created = ref<{ worker_id: string; token: string; name: string; shared: boolean } | null>(null);
const hubUrl = computed(() => location.origin);
const capabilityNames: Record<string, string> = {
  asr: "转写",
  frames: "画面",
  "llm-api": "大模型接口",
  hub: "Hub 本机",
  "agent:dsh": "DSH",
  "agent:codex": "Codex",
  "agent:custom": "自定义 Agent",
};

function label(capability: string): string {
  if (capability.startsWith("resolver:")) return `来源：${capability.slice(9)}`;
  return capabilityNames[capability] ?? capability;
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

async function disable(id: string, label: string): Promise<void> {
  if (
    !(await confirmation.value?.open({
      title: "停用节点",
      message: `确定停用节点「${label}」？`,
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

async function remove(id: string, label: string): Promise<void> {
  if (
    !(await confirmation.value?.open({
      title: "删除节点",
      message: `确定删除节点「${label}」？`,
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
    <div class="page">
      <p class="hint">
        转写、画面与整理由具备相应能力的计算节点完成。节点主动连接 Hub，不需要公网地址；下载的视频留在节点上。
      </p>
      <section class="panel">
        <h3>节点</h3>
        <div class="worker-list">
          <article v-for="worker in workers.data.value?.workers ?? []" :key="worker.id" class="worker-card">
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
        <h3>添加节点</h3>
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
          <p>在节点电脑上运行：</p>
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
