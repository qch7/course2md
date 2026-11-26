<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { client, fileUrl, jobBundleUrl, unwrap } from "@/api/client";
import AppIcon from "@/components/AppIcon.vue";
import BrandMark from "@/components/BrandMark.vue";
import JobConsole from "@/components/JobConsole.vue";
import StagesPanel from "@/components/StagesPanel.vue";
import VersionsPanel from "@/components/VersionsPanel.vue";
import { absorb, isBusy, setSelected, store, watchJob } from "@/stores/jobs";
import { showError, toast, ui } from "@/stores/ui";
import { agentName, backendLabel, statusNames } from "@/utils/format";

const props = defineProps<{ jobId: string }>();
const router = useRouter();
const job = computed(() => store.jobs.get(props.jobId));
const busy = computed(() => isBusy(job.value));
const tab = ref<"chat" | "notes" | "output" | "stages" | "versions">("chat");
const tabs = [
  ["chat", "对话"],
  ["notes", "笔记"],
  ["output", "运行输出"],
  ["stages", "阶段"],
  ["versions", "版本"],
] as const;

// ------------------------------------------------------------------ 对话
const drafts = new Map<string, string>();
const draft = ref(drafts.get(props.jobId) ?? "");
const pending = ref<{ id: string; text: string } | null>(null);
const thread = ref<HTMLElement | null>(null);
const outputConsole = ref<InstanceType<typeof JobConsole> | null>(null);
const sending = ref(false);

const noteFiles = computed(() => (job.value?.files ?? []).filter((file) => file.kind === "notes"));
const bundles = computed(() => (job.value?.files ?? []).filter((file) => file.kind === "bundle"));
const otherFiles = computed(() =>
  (job.value?.files ?? []).filter((file) => !["notes", "bundle"].includes(file.kind ?? "")),
);
const composerHint = computed(() => {
  const current = job.value;
  if (!current) return "";
  if ((current.pending_messages ?? 0) > 0) return `${current.pending_messages} 条要求等待处理`;
  if (busy.value) return "可以继续发送要求，将在当前任务完成后依次处理";
  return current.can_chat
    ? "修改会同步更新详细版、缩略版和配图压缩包"
    : "课程对话需要整理上下文，现有文件可在“笔记”页查看";
});

async function send(): Promise<void> {
  const text = draft.value.trim();
  if (!text || !job.value?.can_chat) return;
  if (!pending.value || pending.value.text !== text) pending.value = { id: crypto.randomUUID(), text };
  sending.value = true;
  try {
    const data = await unwrap(
      client.POST("/api/v1/jobs/{job_id}/messages", {
        params: { path: { job_id: props.jobId } },
        body: pending.value,
      }),
    );
    pending.value = null;
    draft.value = "";
    absorb(data.job);
    watchJob(props.jobId);
    toast(busy.value ? "要求已加入队列" : "已发送");
  } catch (error) {
    showError(error);
  } finally {
    sending.value = false;
  }
}

function onKey(event: KeyboardEvent): void {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    send();
  }
}

async function cancel(): Promise<void> {
  try {
    await unwrap(client.POST("/api/v1/jobs/{job_id}/cancel", { params: { path: { job_id: props.jobId } } }));
    toast("已请求停止");
  } catch (error) {
    showError(error);
  }
}

// ------------------------------------------------------------------ 笔记
const currentNote = ref<string | null>(null);
const preview = ref<{ html?: string | null; text?: string | null; image_url?: string | null; name?: string }>(
  {},
);
const previewState = ref("");
const imageDialog = ref<HTMLDialogElement | null>(null);
const imageSrc = ref("");
const noteBox = ref<HTMLElement | null>(null);
const cache = new Map<string, typeof preview.value>();
let previewRequest = 0;

async function loadNote(): Promise<void> {
  const current = job.value;
  if (!current) return;
  if (!currentNote.value) currentNote.value = noteFiles.value[0]?.rel ?? null;
  if (!currentNote.value) {
    preview.value = {};
    previewState.value = "课程整理完成后，可在这里阅读两版笔记。";
    return;
  }
  const key = `${current.id}:${current.revision}:${currentNote.value}`;
  const request = ++previewRequest;
  previewState.value = "正在打开笔记…";
  try {
    let data = cache.get(key);
    if (!data) {
      data = await unwrap(
        client.GET("/api/v1/jobs/{job_id}/preview", {
          params: { path: { job_id: current.id }, query: { rel: currentNote.value } },
        }),
      );
      cache.set(key, data);
    }
    if (request !== previewRequest) return;
    preview.value = data;
    previewState.value = "";
    await nextTick();
    noteBox.value?.querySelectorAll<HTMLAnchorElement>("a[data-note-rel]").forEach((link) => {
      link.onclick = (event) => {
        event.preventDefault();
        currentNote.value = link.dataset.noteRel ?? null;
      };
    });
    noteBox.value?.querySelectorAll<HTMLImageElement>("img").forEach((image) => {
      image.onclick = () => openImage(image.src);
    });
  } catch (error) {
    if (request === previewRequest) previewState.value = (error as Error).message;
  }
}

function openNote(rel: string): void {
  currentNote.value = rel;
  tab.value = "notes";
}

function openImage(src: string): void {
  imageSrc.value = src;
  imageDialog.value?.showModal();
}

watch([tab, currentNote, () => job.value?.revision], () => {
  if (tab.value === "notes") loadNote();
});

// ------------------------------------------------------------------ 生命周期
watch(
  () => job.value?.messages?.length,
  async () => {
    const element = thread.value;
    if (!element) return;
    const near = element.scrollHeight - element.scrollTop - element.clientHeight < 150;
    await nextTick();
    if (near) element.scrollTop = element.scrollHeight;
  },
);

onMounted(async () => {
  if (!store.jobs.has(props.jobId)) {
    try {
      absorb(
        await unwrap(client.GET("/api/v1/jobs/{job_id}", { params: { path: { job_id: props.jobId } } })),
      );
    } catch (error) {
      showError(error);
      router.replace("/");
      return;
    }
  }
  setSelected(props.jobId);
  await nextTick();
  if (thread.value) thread.value.scrollTop = thread.value.scrollHeight;
});

onBeforeUnmount(() => {
  drafts.set(props.jobId, draft.value);
  setSelected(null);
});
</script>

<template>
  <template v-if="job">
    <header class="topbar">
      <button
        class="icon-button mobile-menu"
        aria-label="展开课程列表"
        @click="ui.sidebarOpen = !ui.sidebarOpen"
      >
        <AppIcon name="menu" :size="19" />
      </button>
      <div class="course-heading">
        <span class="folder-icon" aria-hidden="true"></span>
        <h1 :title="job.title">{{ job.title }}</h1>
      </div>
      <div class="topbar-actions">
        <RouterLink
          v-if="!busy && (job.files?.length ?? 0) > 0"
          class="quiet-button"
          :to="{ name: 'transfer', query: { jobId: job.id } }"
          >互传</RouterLink
        >
        <RouterLink
          v-if="job.course_id && store.courses.has(job.course_id)"
          class="quiet-button"
          :to="{ name: 'course', params: { courseId: job.course_id } }"
          >下载其他节次</RouterLink
        >
        <span class="backend-indicator">{{ backendLabel(job) }}</span>
        <button v-if="busy" class="quiet-button" @click="cancel">停止</button>
      </div>
    </header>
    <div class="workspace-content">
      <section class="course-view">
        <div class="view-toolbar">
          <div class="view-tabs" role="tablist" aria-label="课程视图">
            <button
              v-for="[name, label] in tabs"
              :key="name"
              role="tab"
              :aria-selected="tab === name"
              @click="tab = name"
            >
              {{ label }}<i v-if="name === 'output' && busy"></i>
            </button>
          </div>
          <span class="job-state" aria-live="polite">
            {{
              busy
                ? `${job.stage} · ${Math.round((job.progress ?? 0) * 100)}%`
                : statusNames[job.status ?? ""]
            }}
          </span>
        </div>
        <div v-if="busy" class="progress-track">
          <i :style="{ width: `${Math.round((job.progress ?? 0) * 100)}%` }"></i>
        </div>

        <div v-show="tab === 'chat'" class="chat-view" role="tabpanel">
          <div ref="thread" class="thread-scroll">
            <div class="thread">
              <div class="messages">
                <article
                  v-for="message in job.messages"
                  :key="message.id"
                  class="message"
                  :class="[message.role, { failed: message.status === 'failed' }]"
                >
                  <template v-if="message.role === 'assistant'">
                    <div class="message-label"><BrandMark />{{ agentName(job.agent) }}</div>
                    <div v-if="message.html" v-html="message.html"></div>
                    <div v-else>{{ message.text }}</div>
                  </template>
                  <template v-else>{{ message.text }}</template>
                  <small
                    v-if="['queued', 'running', 'interrupted'].includes(message.status ?? '')"
                    class="message-status"
                  >
                    {{
                      { queued: "等待处理", running: "正在处理", interrupted: "上次处理已中断，可重新发送" }[
                        message.status as "queued" | "running" | "interrupted"
                      ]
                    }}
                  </small>
                </article>
              </div>
              <div class="result-files">
                <button
                  v-for="file in noteFiles"
                  :key="file.rel"
                  class="artifact"
                  @click="openNote(file.rel ?? '')"
                >
                  <span class="artifact-icon"><AppIcon name="file" :size="20" /></span>
                  <span>
                    <strong>{{ file.name?.endsWith(".brief.md") ? "缩略版" : "详细版" }}</strong>
                    <small
                      >{{ file.name?.endsWith(".brief.md") ? "重点与例题结论" : "例题、推导与课堂画面" }} ·
                      {{ file.size }}</small
                    >
                  </span>
                </button>
                <a
                  v-for="file in bundles"
                  :key="file.rel"
                  class="bundle-link"
                  :href="fileUrl(job.id, file.rel ?? '')"
                >
                  下载两版笔记与配图（ZIP，{{ file.size }}）
                </a>
                <details v-if="otherFiles.length" class="other-files">
                  <summary>转写稿与其他文件（{{ otherFiles.length }}）</summary>
                  <div v-for="file in otherFiles" :key="file.rel" class="other-file">
                    <span :title="file.rel">{{ file.name }}</span>
                    <button
                      v-if="!['media', 'meta'].includes(file.kind ?? '')"
                      @click="openNote(file.rel ?? '')"
                    >
                      预览
                    </button>
                    <a :href="fileUrl(job.id, file.rel ?? '')">下载</a>
                  </div>
                </details>
              </div>
              <details v-if="busy || job.events?.length" class="live-panel" :class="{ busy }" :open="busy">
                <summary>
                  <span><i class="activity-dot"></i> Agent 实时输出</span>
                  <span>{{ busy ? "运行中" : "本轮结束" }}</span>
                </summary>
                <JobConsole :events="(job.events ?? []).slice(-60)" compact />
              </details>
              <div v-if="job.error" class="notice error-notice">{{ job.error }}</div>
            </div>
          </div>
          <div class="composer-wrap">
            <form class="composer" @submit.prevent="send">
              <label class="sr-only" for="chat-input">向课程提问或要求修改笔记</label>
              <textarea
                id="chat-input"
                v-model="draft"
                rows="2"
                :disabled="!job.can_chat"
                placeholder="问这节课，或描述要修改的内容…"
                @keydown="onKey"
              ></textarea>
              <div class="composer-footer">
                <span>{{ agentName(job.agent) }}</span>
                <div>
                  <span class="composer-shortcut">Enter 发送 · Shift + Enter 换行</span>
                  <button
                    type="submit"
                    class="send-button"
                    aria-label="发送消息"
                    :disabled="!job.can_chat || !draft.trim() || sending"
                  >
                    <AppIcon name="up" :size="18" />
                  </button>
                </div>
              </div>
            </form>
            <div class="composer-hint">{{ composerHint }}</div>
          </div>
        </div>

        <div v-if="tab === 'notes'" class="notes-view" role="tabpanel">
          <div class="notes-toolbar">
            <div class="note-switch">
              <button
                v-for="file in noteFiles"
                :key="file.rel"
                :class="{ active: file.rel === currentNote }"
                @click="currentNote = file.rel ?? null"
              >
                {{ file.name?.endsWith(".brief.md") ? "缩略版" : "详细版" }}
              </button>
            </div>
            <div class="note-downloads">
              <a
                v-if="noteFiles.length && job.can_chat"
                class="quiet-button"
                :href="`/api/v1/jobs/${encodeURIComponent(job.id)}/export/anki`"
              >
                导出 Anki 卡片
              </a>
              <a v-if="currentNote" class="quiet-button" :href="fileUrl(job.id, currentNote)">
                下载 Markdown
              </a>
              <a
                v-if="!busy && (job.files?.length ?? 0) > 0"
                class="quiet-button"
                :href="jobBundleUrl(job.id)"
              >
                导出产物包
              </a>
            </div>
          </div>
          <div ref="noteBox" class="note-content markdown-preview">
            <template v-if="previewState">{{ previewState }}</template>
            <div v-else-if="preview.html" v-html="preview.html"></div>
            <img
              v-else-if="preview.image_url"
              :src="preview.image_url"
              :alt="preview.name"
              @click="openImage(preview.image_url)"
            />
            <pre v-else>{{ preview.text }}</pre>
          </div>
        </div>

        <div v-if="tab === 'output'" class="output-view" role="tabpanel">
          <div class="output-toolbar">
            <span>{{ backendLabel(job) }} · 运行事件与文本输出</span>
            <button class="quiet-button" @click="outputConsole?.scrollToEnd()">跳到最新输出</button>
          </div>
          <JobConsole ref="outputConsole" :events="job.events ?? []" />
          <details class="stage-log">
            <summary>提取进度日志</summary>
            <pre>{{ (job.logs ?? []).join("\n") }}</pre>
          </details>
        </div>

        <StagesPanel v-if="tab === 'stages'" :job="job" />
        <VersionsPanel v-if="tab === 'versions'" :job="job" />
      </section>
    </div>
    <dialog ref="imageDialog" class="image-dialog">
      <form method="dialog"><button class="icon-button image-close" aria-label="关闭图片">×</button></form>
      <img :src="imageSrc" alt="课堂画面" />
    </dialog>
  </template>
  <div v-else class="loading-screen">正在打开课程…</div>
</template>
