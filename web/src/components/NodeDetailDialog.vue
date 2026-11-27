<script setup lang="ts">
import { computed, ref, useId, watch } from "vue";

import type { HubLink, LocalNode, Worker } from "@/api/client";
import { groupCapabilities } from "@/utils/capabilities";
import { dateTime } from "@/utils/format";

export interface HubDetail {
  kind: "hub";
  name: string;
  address: string;
  mode: string;
  capabilities: string[];
  localOnline: number;
  localTotal: number;
  remoteOnline: number;
  remoteTotal: number;
  externalOnline: number;
  externalTotal: number;
}

export type DetailTarget =
  | HubDetail
  | { kind: "local"; node: LocalNode; canManage: boolean }
  | { kind: "remote"; worker: Worker; canManage: boolean }
  | { kind: "external"; link: HubLink; canManage: boolean };

export type DetailAction =
  | "edit-local"
  | "toggle-local"
  | "delete-local"
  | "disable-worker"
  | "delete-worker"
  | "connect-link"
  | "disconnect-link"
  | "edit-link"
  | "delete-link"
  | "add-local";

const props = defineProps<{ target: DetailTarget | null; busy?: boolean }>();
const emit = defineEmits<{ close: []; action: [action: DetailAction] }>();

const dialog = ref<HTMLDialogElement | null>(null);
const titleId = useId();

watch(
  () => props.target !== null,
  (open) => {
    const element = dialog.value;
    if (!element) return;
    if (open && !element.open) element.showModal();
    if (!open && element.open) element.close();
  },
  { flush: "post" },
);

const KIND_TITLES = {
  hub: "本机 Hub",
  local: "本机节点",
  remote: "连入本机的节点",
  external: "外部 Hub",
} as const;

const KIND_SUMMARY = {
  hub: "网状计算的中心：接收任务、调度到下面的节点，并保存课程与笔记。",
  local: "与 Hub 同进程运行，可读取本机文件；所有本机节点共享显存与模型，一次只执行一个阶段。",
  remote: "其它电脑上的计算节点，主动连接到本机 Hub 领取任务，下载的视频留在节点上。",
  external: "本机作为计算节点连入的另一个 Hub，为它提供转写、画面与整理能力。",
} as const;

const linkStatusLabels: Record<string, string> = {
  online: "在线",
  busy: "执行中",
  connecting: "连接中…",
  reconnecting: "重连中…",
  auth_failed: "令牌失效",
  disconnected: "未连接",
};

function hubHost(url?: string): string {
  if (!url) return "";
  try {
    return new URL(url).host || url;
  } catch {
    return url;
  }
}

const model = computed(() => {
  const target = props.target;
  if (!target) return null;
  switch (target.kind) {
    case "hub":
      return {
        title: target.name,
        status: "on",
        statusText: "运行中",
        rows: [
          ["访问地址", target.address],
          ["运行形态", target.mode],
          ["本机节点", `${target.localOnline} / ${target.localTotal} 在线`],
          ["连入的节点", `${target.remoteOnline} / ${target.remoteTotal} 在线`],
          ["外部 Hub", `${target.externalOnline} / ${target.externalTotal} 在线`],
        ],
        capabilities: target.capabilities,
        capabilityTitle: "本机可提供的能力",
      };
    case "local": {
      const node = target.node;
      const status = !node.enabled ? "off" : node.busy ? "busy" : node.online ? "on" : "off";
      return {
        title: node.name || node.id,
        status,
        statusText: !node.enabled ? "已停用" : node.busy ? "执行中" : node.online ? "在线" : "离线",
        rows: [
          ["节点编号", node.id],
          ["能力来源", node.auto ? "自动跟随本机检测结果" : "手动选择"],
          ...(node.current_task ? [["正在执行", node.current_task]] : []),
          ...(node.builtin ? [["类型", "内置节点（不可删除）"]] : []),
        ],
        capabilities: node.capabilities ?? [],
        capabilityTitle: "承担的能力",
      };
    }
    case "remote": {
      const worker = target.worker;
      return {
        title: worker.name || worker.id,
        status: worker.online ? "on" : "off",
        statusText: worker.online ? "在线" : worker.disabled ? "已停用" : "离线",
        rows: [
          ["节点编号", worker.id],
          ["归属", worker.owner_id ? "个人节点" : "共享节点"],
          ...(worker.version ? [["版本", worker.version]] : []),
          ["最近在线", worker.last_seen ? dateTime(worker.last_seen) : "尚未连接"],
        ],
        capabilities: worker.capabilities ?? [],
        capabilityTitle: "承担的能力",
      };
    }
    default: {
      const link = target.link;
      const status = link.status ?? "disconnected";
      return {
        title: hubHost(link.hub_url),
        status:
          status === "online"
            ? "on"
            : status === "busy"
              ? "busy"
              : status === "connecting" || status === "reconnecting"
                ? "warn"
                : status === "auth_failed"
                  ? "err"
                  : "off",
        statusText: linkStatusLabels[status] ?? status,
        rows: [
          ["Hub 地址", link.hub_url ?? ""],
          ["上报名称", link.name || "（默认主机名）"],
          ...(link.worker_id ? [["在该 Hub 的节点编号", link.worker_id]] : []),
          ["来源", link.source === "env" ? "来自 .env" : "手动添加"],
          ["令牌", link.token_hint || "未保存"],
          ["启动时自动连接", link.auto_connect ? "是" : "否"],
          ["最近心跳", link.last_heartbeat ? dateTime(link.last_heartbeat) : "尚无"],
          ["已完成 / 失败", `${link.completed_tasks ?? 0} / ${link.failed_tasks ?? 0} 次`],
        ],
        capabilities: link.capabilities ?? [],
        capabilityTitle: "向该 Hub 提供的能力",
      };
    }
  }
});

const groups = computed(() => groupCapabilities(model.value?.capabilities ?? []));
const link = computed(() => (props.target?.kind === "external" ? props.target.link : null));
</script>

<template>
  <dialog
    ref="dialog"
    class="node-dialog"
    :aria-labelledby="titleId"
    @close="emit('close')"
    @cancel.prevent="emit('close')"
    @click.self="emit('close')"
  >
    <template v-if="target && model">
      <div class="dialog-header">
        <div class="node-dialog-title">
          <span class="kind-chip" :class="`kind-${target.kind}`">{{ KIND_TITLES[target.kind] }}</span>
          <h2 :id="titleId">{{ model.title }}</h2>
          <p>
            <span class="online" :class="model.status"></span>
            {{ model.statusText }}
          </p>
        </div>
        <button type="button" class="icon-button" aria-label="关闭" @click="emit('close')">×</button>
      </div>

      <div class="node-dialog-body">
        <p class="hint">{{ KIND_SUMMARY[target.kind] }}</p>

        <dl class="detail-grid">
          <template v-for="[key, value] in model.rows" :key="key">
            <dt>{{ key }}</dt>
            <dd>{{ value }}</dd>
          </template>
        </dl>

        <div v-if="link?.current_task" class="link-busy">
          正在执行阶段 <strong>{{ link.current_task.stage }}</strong>（任务 {{ link.current_task.job_id }}）
        </div>
        <div v-if="link?.last_error" class="link-error">{{ link.last_error }}</div>

        <section class="detail-capabilities">
          <h3>{{ model.capabilityTitle }}</h3>
          <p v-if="!groups.length" class="muted">暂无（节点尚未连接，或没有可承担的能力）</p>
          <div v-for="group in groups" :key="group.key" class="capability-group">
            <span class="capability-group-title">{{ group.title }}</span>
            <div class="capabilities">
              <span
                v-for="item in group.items"
                :key="item.name"
                class="tag"
                :title="item.hint || item.name"
                >{{ item.label }}</span
              >
            </div>
          </div>
        </section>

        <details v-if="link?.logs?.length" class="detail-logs">
          <summary>连接日志（{{ link.logs.length }}）</summary>
          <pre class="link-logs">{{ link.logs.join("\n") }}</pre>
        </details>
      </div>

      <div class="dialog-footer">
        <template v-if="target.kind === 'hub'">
          <button type="button" class="quiet-button" @click="emit('action', 'add-local')">
            新建本机节点
          </button>
        </template>
        <template v-else-if="target.kind === 'local' && target.canManage">
          <button
            v-if="!target.node.builtin"
            type="button"
            class="quiet-button danger"
            :disabled="busy"
            @click="emit('action', 'delete-local')"
          >
            删除
          </button>
          <button type="button" class="quiet-button" :disabled="busy" @click="emit('action', 'toggle-local')">
            {{ target.node.enabled ? "停用" : "启用" }}
          </button>
          <button type="button" class="primary-button" :disabled="busy" @click="emit('action', 'edit-local')">
            编辑名称与能力
          </button>
        </template>
        <template v-else-if="target.kind === 'remote' && target.canManage">
          <button
            v-if="!target.worker.disabled"
            type="button"
            class="quiet-button"
            :disabled="busy"
            @click="emit('action', 'disable-worker')"
          >
            停用
          </button>
          <button
            type="button"
            class="quiet-button danger"
            :disabled="busy"
            @click="emit('action', 'delete-worker')"
          >
            删除
          </button>
        </template>
        <template v-else-if="target.kind === 'external' && target.canManage">
          <button
            type="button"
            class="quiet-button danger"
            :disabled="busy"
            @click="emit('action', 'delete-link')"
          >
            移除
          </button>
          <button type="button" class="quiet-button" :disabled="busy" @click="emit('action', 'edit-link')">
            编辑
          </button>
          <button
            v-if="link?.status === 'disconnected' || link?.status === 'auth_failed'"
            type="button"
            class="primary-button"
            :disabled="busy"
            @click="emit('action', 'connect-link')"
          >
            连接
          </button>
          <button
            v-else
            type="button"
            class="quiet-button"
            :disabled="busy"
            @click="emit('action', 'disconnect-link')"
          >
            断开
          </button>
        </template>
        <button type="button" class="quiet-button" @click="emit('close')">关闭</button>
      </div>
    </template>
  </dialog>
</template>
