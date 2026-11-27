<script setup lang="ts">
import { reactive, ref } from "vue";

import { client, type HubLink, unwrap } from "@/api/client";
import type ConfirmDialog from "@/components/ConfirmDialog.vue";
import { session } from "@/stores/session";
import { showError, toast } from "@/stores/ui";
import { hubHost, linkDotClass, linkStatusLabels, parseWorkerCommand } from "@/utils/mesh";

// 外部 Hub 区块：本机作为计算节点连出的远程 Hub 列表与增删改、连接控制。
// 列表数据由父组件的查询提供，改动后通过 changed 通知父组件刷新。
// confirm 是页面共用的确认对话框，避免页面里出现两个确认框实例
const props = defineProps<{
  links: HubLink[];
  defaultName?: string;
  defaultWorkDir?: string;
  confirm: InstanceType<typeof ConfirmDialog> | null;
}>();
const emit = defineEmits<{ changed: [] }>();

const showAdd = ref(false);
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

function handleHubInput(target: { hub_url: string; token: string; name: string; work_dir: string }): void {
  const parsed = parseWorkerCommand(target.hub_url);
  if (!parsed) return;
  if (parsed.hub_url) target.hub_url = parsed.hub_url;
  if (parsed.token) target.token = parsed.token;
  if (parsed.name) target.name = parsed.name;
  if (parsed.work_dir) target.work_dir = parsed.work_dir;
  toast("已从命令自动填入 Hub 地址与令牌");
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
    showAdd.value = false;
    toast("已添加并开始连接远程 Hub");
    emit("changed");
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
    emit("changed");
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
    emit("changed");
  } catch (error) {
    showError(error);
  } finally {
    busyLink.value = null;
  }
}

async function disconnectLink(link: HubLink): Promise<void> {
  if (link.status === "busy") {
    const confirmed = await props.confirm?.open({
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
    emit("changed");
  } catch (error) {
    showError(error);
  } finally {
    busyLink.value = null;
  }
}

async function deleteLink(link: HubLink): Promise<void> {
  const confirmed = await props.confirm?.open({
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
    emit("changed");
  } catch (error) {
    showError(error);
  } finally {
    busyLink.value = null;
  }
}

// 供父组件在拓扑图详情弹窗里触发同样的操作
defineExpose({
  openAdd: () => {
    showAdd.value = true;
  },
  busy: busyLink,
  connect: connectLink,
  disconnect: disconnectLink,
  remove: deleteLink,
  edit: startEdit,
});
</script>

<template>
  <section id="section-external-hubs" class="node-section">
    <div class="section-head">
      <h2>
        外部 Hub<span class="pill">{{ links.length }}</span>
      </h2>
      <button class="quiet-button small" @click="showAdd = !showAdd">
        {{ showAdd ? "收起" : "+ 连接新 Hub" }}
      </button>
    </div>
    <p class="hint">
      本机作为计算节点连入的其它 Hub。在 <code>.env</code> 中配置 <code>COURSE2MD_HUB_URL</code> 与
      <code>COURSE2MD_WORKER_TOKEN</code> 后，启动本地服务会自动连入。
    </p>

    <div v-if="links.length" class="worker-list">
      <article
        v-for="link in links"
        :id="`link-${link.id}`"
        :key="link.id"
        class="node-card worker-card hub-link-card"
      >
        <div class="worker-head">
          <span class="node-icon kind-external" aria-hidden="true"></span>
          <div class="node-name">
            <div class="node-title">
              <a
                class="hub-link-url"
                :href="`${link.hub_url}/workers`"
                target="_blank"
                rel="noopener noreferrer"
                >{{ hubHost(link.hub_url) }}</a
              >
              <span class="pill">{{ link.source === "env" ? "来自 .env" : "手动添加" }}</span>
            </div>
            <small class="node-meta"
              >{{ link.name }}{{ link.worker_id ? ` · ${link.worker_id}` : ""
              }}{{ link.token_hint ? ` · 令牌 ${link.token_hint}` : ""
              }}{{ link.auto_connect ? " · 启动自动连" : "" }}</small
            >
          </div>
          <span class="status-chip"
            ><span class="online" :class="linkDotClass(link.status)"></span
            >{{ linkStatusLabels[link.status ?? "disconnected"] ?? link.status }}</span
          >
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
          正在执行阶段 <strong>{{ link.current_task.stage }}</strong>（任务 {{ link.current_task.job_id }}）
        </div>
        <div v-if="link.last_error" class="link-error">
          {{ link.last_error }}
        </div>

        <CapabilityTags :capabilities="link.capabilities" />

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
              <input v-model="editForm.token" type="password" placeholder="留空保持不变" autocomplete="off" />
            </label>
            <label class="field">
              <span>节点名称</span>
              <input v-model="editForm.name" :placeholder="defaultName || '我的电脑'" />
            </label>
            <label v-if="session.mode === 'local'" class="field">
              <span>工作目录</span>
              <input v-model="editForm.work_dir" :placeholder="defaultWorkDir || 'course2md-worker'" />
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
            <button class="primary-button small" :disabled="busyLink === link.id" @click="saveEdit(link.id)">
              保存配置
            </button>
          </div>
        </div>

        <div v-if="link.logs?.length" class="link-logs-wrap">
          <button type="button" class="text-button" @click="expandedLogs[link.id] = !expandedLogs[link.id]">
            {{ expandedLogs[link.id] ? "收起日志" : `查看日志（${link.logs.length}）` }}
          </button>
          <pre v-if="expandedLogs[link.id]" class="link-logs">{{ link.logs.join("\n") }}</pre>
        </div>
      </article>
    </div>

    <p v-else-if="!showAdd" class="node-empty">
      尚未连接到外部 Hub。点击右上角「+ 连接新 Hub」，或在 <code>.env</code> 配置后重启即可自动连入。
    </p>

    <form v-if="showAdd || !links.length" class="hub-add-form" @submit.prevent="addHubLink">
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
          <input v-model="newLink.token" type="password" placeholder="c2m_..." required autocomplete="off" />
        </label>
        <label class="field">
          <span>节点名称（可选）</span>
          <input v-model="newLink.name" :placeholder="defaultName || '我的电脑'" />
        </label>
        <label v-if="session.mode === 'local'" class="field">
          <span>工作目录（可选）</span>
          <input v-model="newLink.work_dir" :placeholder="defaultWorkDir || 'course2md-worker'" />
        </label>
      </div>
      <div class="inline-form">
        <label class="check-inline">
          <input v-model="newLink.auto_connect" type="checkbox" /> 启动时自动连接
        </label>
        <label class="check-inline"> <input v-model="newLink.no_asr" type="checkbox" /> 不承担转写 </label>
        <label class="check-inline">
          <input v-model="newLink.no_agents" type="checkbox" /> 不承担 Agent 整理
        </label>
        <button type="submit" class="primary-button" :disabled="busyLink === 'new'">
          {{ busyLink === "new" ? "连接中…" : "添加并连接" }}
        </button>
      </div>
    </form>
  </section>
</template>
