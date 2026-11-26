<script lang="ts">
// 解析后跳转到 /course/:id 会重新创建组件；刚解析过的课程不必立即再刷新一次。
let justResolved = "";
</script>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { ApiError, client, type Course, type CreateJobRequest, type Session, unwrap } from "@/api/client";
import AppIcon from "@/components/AppIcon.vue";
import BrandMark from "@/components/BrandMark.vue";
import OptionsDialog from "@/components/OptionsDialog.vue";
import { absorb, absorbCourse, dropJob, isBusy, store, watchJob } from "@/stores/jobs";
import { isAdmin, options, session } from "@/stores/session";
import { showError, toast, ui } from "@/stores/ui";
import { agentName } from "@/utils/format";
import { weekFor } from "@/utils/week";

const props = defineProps<{ courseId?: string }>();
const router = useRouter();

const source = ref("");
const status = ref("");
const resolving = ref(false);
const submitting = ref(false);
const editingSource = ref(!props.courseId);
const course = ref<Course | null>(null);
const termStart = ref(session.config?.term_start ?? "");
const selected = ref(new Set<string>());
const optionsDialog = ref<InstanceType<typeof OptionsDialog> | null>(null);
const overwriteDialog = ref<HTMLDialogElement | null>(null);
const overwrite = ref<{ existing: Record<string, unknown>[]; selected: unknown[] } | null>(null);
let overwriteResolve: ((choice: string) => void) | null = null;

const sessions = computed<Session[]>(() => course.value?.sessions ?? []);
const title = computed(() => (course.value ? course.value.title : "新建课程"));
const supportsLocalSource = computed(
  () => !session.config?.server || Boolean(session.config?.has_local_node && isAdmin()),
);
const visibleResolvers = computed(() =>
  (session.config?.resolvers ?? []).filter((item) => item.name !== "local" || supportsLocalSource.value),
);
const resolverLabels = computed(() => visibleResolvers.value.map((item) => item.label));
const agents = computed(() => Object.keys(session.config?.agents ?? {}));

function sessionBusy(item: Session): boolean {
  const job = [...store.jobs.values()].find((entry) => entry.out_dir === item.folder);
  return job ? isBusy(job) : ["queued", "running"].includes(item.status ?? "");
}

const available = computed(() => sessions.value.filter((item) => !sessionBusy(item)));
const chosenCount = computed(
  () => available.value.filter((item) => selected.value.has(item.id ?? "")).length,
);
const allChosen = computed(() => available.value.length > 0 && chosenCount.value === available.value.length);

function apply(data: Course): void {
  course.value = data;
  absorbCourse(data);
  termStart.value = data.term_start ?? "";
  options.views = (data.view_options ?? []).filter((item) => item.default).map((item) => Number(item.id));
  selected.value = new Set(
    (data.sessions ?? []).filter((item) => !item.existing && !sessionBusy(item)).map((item) => item.id ?? ""),
  );
}

async function refresh(): Promise<void> {
  if (!props.courseId) return;
  resolving.value = true;
  status.value = "正在刷新课次…";
  try {
    const data = await unwrap(
      client.POST("/api/v1/courses/{course_id}/refresh", {
        params: { path: { course_id: props.courseId } },
        body: {},
      }),
    );
    apply(data);
    status.value = `共 ${sessions.value.length} 个课次，可勾选后继续下载整理`;
  } catch (error) {
    status.value = "已显示上次保存的课次";
    showError(`${(error as Error).message}。可通过“更新来源链接”重新解析。`);
  } finally {
    resolving.value = false;
  }
}

async function resolve(): Promise<void> {
  const text = source.value.trim();
  if (!text) return;
  showError("");
  resolving.value = true;
  status.value = "正在读取课程信息…";
  try {
    const data = await unwrap(
      client.POST("/api/v1/resolve", {
        body: {
          source: text,
          out_dir: options.outDir || null,
          term_start: termStart.value || null,
          course_id: props.courseId || null,
        },
      }),
    );
    apply(data);
    const id = data.course_id || data.id;
    status.value = data.sessions?.length
      ? `已识别 ${data.resolver_label || data.resolver} · ${data.sessions.length} 个课次`
      : "该来源可直接提交整理";
    editingSource.value = false;
    if (id && id !== props.courseId) {
      justResolved = id;
      router.replace({ name: "course", params: { courseId: id } });
    }
  } catch (error) {
    status.value = "";
    showError(error);
  } finally {
    resolving.value = false;
  }
}

function toggleAll(checked: boolean): void {
  selected.value = new Set(checked ? available.value.map((item) => item.id ?? "") : []);
}

function toggle(id: string, checked: boolean): void {
  const next = new Set(selected.value);
  if (checked) next.add(id);
  else next.delete(id);
  selected.value = next;
}

function payload(ids: string[], replace: boolean): CreateJobRequest {
  const choice = options.agentChoices[options.agent];
  return {
    source: course.value?.source || source.value.trim(),
    session_ids: ids,
    course_id: course.value?.id ?? null,
    course: course.value?.course || course.value?.title || null,
    term_start: termStart.value || null,
    views: course.value?.view_options?.length ? options.views : null,
    out_dir: options.outDir || null,
    formats: options.formats,
    model: options.model,
    device: options.device,
    ai: options.refine,
    proofread: options.proofread && options.refine,
    detail: options.detail,
    frames: options.frames,
    frame_selector: options.frameSelector || null,
    keep_audio: options.keepAudio,
    delete_video: options.deleteVideo,
    agent: options.agent,
    llm_base: options.llmBase || null,
    llm_model: options.llmModel || null,
    ...(choice ? { agent_provider: choice.provider, agent_model: choice.model } : {}),
    overwrite: replace,
  };
}

function confirmOverwrite(data: Record<string, unknown>): Promise<string> {
  overwrite.value = {
    existing: (data.existing as Record<string, unknown>[]) ?? [],
    selected: (data.selected as unknown[]) ?? [],
  };
  overwriteDialog.value!.returnValue = "";
  overwriteDialog.value!.showModal();
  return new Promise((resolveChoice) => (overwriteResolve = resolveChoice));
}

function overwriteClosed(): void {
  overwriteResolve?.(overwriteDialog.value?.returnValue || "cancel");
  overwriteResolve = null;
}

async function start(): Promise<void> {
  if (!options.formats.length) return showError("请至少选择一种转写输出格式");
  let ids = sessions.value.length
    ? available.value.filter((item) => selected.value.has(item.id ?? "")).map((item) => item.id ?? "")
    : [];
  if (sessions.value.length && !ids.length) return showError("请至少选择一个课次");
  submitting.value = true;
  showError("");
  try {
    let data;
    try {
      data = await unwrap(client.POST("/api/v1/jobs", { body: payload(ids, false) }));
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 409 || !error.payload.requires_confirmation)
        throw error;
      const choice = await confirmOverwrite(error.payload);
      if (choice === "cancel") return;
      if (choice === "skip") {
        const skip = new Set(overwrite.value!.existing.map((item) => String(item.session_id ?? "")));
        ids = ids.filter((id) => !skip.has(id));
        if (!ids.length) return toast("选中的课次都已有结果");
      }
      data = await unwrap(client.POST("/api/v1/jobs", { body: payload(ids, choice === "overwrite") }));
    }
    if (data.course) absorbCourse(data.course);
    const created = data.jobs ?? [];
    for (const job of created) {
      [...store.jobs.values()].forEach((existing) => {
        if (existing.id !== job.id && existing.out_dir === job.out_dir) dropJob(existing.id);
      });
      absorb(job);
      watchJob(job.id);
    }
    if (created[0]) {
      toast(created.length > 1 ? `已加入 ${created.length} 个课次` : "已加入整理队列");
      router.push({ name: "job", params: { jobId: created[0].id } });
    }
  } catch (error) {
    showError(error);
  } finally {
    submitting.value = false;
  }
}

const agentHint = computed(() => {
  if (options.agent === "codex")
    return `本地筛选课件代表图，Codex 每批最多读取 ${session.config?.max_images ?? 12} 张，生成两版笔记。`;
  if (options.agent === "dsh") return "本地筛选课件代表图，DeepSeek Harness 分批读取后生成两版笔记。";
  return session.config?.llm_vision
    ? "内置 API 已启用视觉输入，读取本地筛选的课件代表图后整理。"
    : "内置模型依据转写整理，截图可在笔记中回看。";
});

onMounted(() => {
  if (!props.courseId) return;
  const saved = store.courses.get(props.courseId);
  if (saved) {
    apply(saved);
    source.value = saved.source ?? "";
  }
  if (justResolved === props.courseId) justResolved = "";
  else refresh();
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
    <div class="course-heading">
      <span class="folder-icon" aria-hidden="true"></span>
      <h1>{{ course ? title : "课程笔记" }}</h1>
    </div>
    <span v-if="!course" class="topbar-note">一点一滴，学有所获</span>
    <div v-else class="topbar-actions">
      <RouterLink class="quiet-button" :to="{ name: 'transfer', query: { courseId: course.id } }">
        互传 / 导出
      </RouterLink>
    </div>
  </header>
  <div class="workspace-content">
    <section class="new-view" :class="{ 'has-course': course }">
      <div class="new-content">
        <div class="new-intro" :class="{ 'course-intro': course }">
          <template v-if="!course">
            <BrandMark class="welcome-mark" />
            <p class="intro-eyebrow">给知识一点沉淀的时间</p>
          </template>
          <h2>{{ course ? course.title : "让课堂，留下来。" }}</h2>
          <p class="intro-description">
            {{
              course
                ? "选好这次想读懂的课次，剩下的交给我们。"
                : "从一段回放开始，把听过的内容变成读懂的笔记。"
            }}
          </p>
        </div>
        <div v-if="course" class="course-actions">
          <button type="button" class="quiet-button" :disabled="resolving" @click="refresh">刷新课次</button>
          <button type="button" class="quiet-button" @click="editingSource = true">更新来源链接</button>
        </div>
        <form v-if="editingSource" class="source-card" :aria-busy="resolving" @submit.prevent="resolve">
          <div class="source-heading">
            <AppIcon name="link" :size="16" /><span>从这里开始</span
            ><span class="source-format">{{ supportsLocalSource ? "链接 / 本地视频" : "课程链接" }}</span>
          </div>
          <label class="sr-only" for="source">课程回放链接或本地视频路径</label>
          <textarea
            id="source"
            v-model="source"
            rows="3"
            :placeholder="supportsLocalSource ? '粘贴课程回放链接，或输入本地视频路径…' : '粘贴课程回放链接…'"
            required
            @keydown.ctrl.enter.prevent="!resolving && resolve()"
            @keydown.meta.enter.prevent="!resolving && resolve()"
          ></textarea>
          <div class="source-actions">
            <span :title="visibleResolvers.map((item) => `${item.label}：${item.hint}`).join('\n')">
              {{
                resolverLabels.length
                  ? `支持${resolverLabels.join("、")}`
                  : supportsLocalSource
                    ? "支持本地视频"
                    : "请先在服务器配置来源解析插件"
              }}
            </span>
            <div class="source-submit">
              <kbd class="source-shortcut">Ctrl / ⌘ + Enter</kbd>
              <button
                class="quiet-button resolve-button"
                type="submit"
                :disabled="resolving || !source.trim()"
              >
                <AppIcon v-if="resolving" name="loader" :size="15" class="spinning" />
                {{ resolving ? "读取中" : "解析来源" }}
                <AppIcon v-if="!resolving" name="arrow" :size="15" />
              </button>
            </div>
          </div>
        </form>
        <div v-if="status" class="resolve-status" aria-live="polite">
          <AppIcon v-if="resolving" name="loader" :size="14" class="spinning" />{{ status }}
        </div>
        <div v-if="sessions.length" class="sessions-bar">
          <label class="bulk-select">
            <input
              type="checkbox"
              :checked="allChosen"
              :indeterminate="chosenCount > 0 && !allChosen"
              :disabled="!available.length"
              @change="toggleAll(($event.target as HTMLInputElement).checked)"
            />
            <span>全选</span>
          </label>
          <span class="selection-count">已选 {{ chosenCount }} / {{ sessions.length }} 个课次</span>
          <label v-if="sessions.some((item) => item.date)" class="term-start" title="教学周从周一开始计算">
            <span>第 1 周从</span><input v-model="termStart" type="date" /><span>起算</span>
          </label>
        </div>
        <div class="sessions">
          <label v-for="item in sessions" :key="item.id" class="session-option">
            <input
              type="checkbox"
              :disabled="sessionBusy(item)"
              :checked="selected.has(item.id ?? '')"
              @change="toggle(item.id ?? '', ($event.target as HTMLInputElement).checked)"
            />
            <span class="session-text">
              <span class="session-title">
                {{ item.title }}
                <b v-if="weekFor(item, termStart)" class="week-badge">第 {{ weekFor(item, termStart) }} 周</b>
              </span>
              <small>
                {{ [item.date, item.views ? `${item.views} 路机位` : ""].filter(Boolean).join(" · ") }}
                <template v-if="sessionBusy(item)"> · 已在整理队列中</template>
                <em
                  v-if="item.existing"
                  class="existing-badge"
                  :title="`${item.existing.path}\n开始整理前会再次确认`"
                >
                  {{ item.existing.complete ? "已有结果" : "有未完成结果" }}
                </em>
              </small>
            </span>
          </label>
        </div>
        <section class="launch-panel" aria-label="整理设置">
          <div class="launch-row">
            <label class="backend-select">
              <span>整理方式</span>
              <select v-model="options.agent">
                <option value="">内置模型</option>
                <option v-for="name in agents" :key="name" :value="name">{{ agentName(name) }}</option>
              </select>
            </label>
            <button class="quiet-button options-button" type="button" @click="optionsDialog?.open()">
              <AppIcon name="settings" :size="16" />整理选项
            </button>
            <button
              class="primary-button start-button"
              :disabled="
                submitting || !(course || source.trim()) || (sessions.length > 0 && chosenCount === 0)
              "
              @click="start"
            >
              <AppIcon v-if="submitting" name="loader" :size="16" class="spinning" />
              {{
                submitting
                  ? "正在提交…"
                  : sessions.length > 1 && chosenCount > 1
                    ? `开始整理 ${chosenCount} 个课次`
                    : "开始整理"
              }}
              <AppIcon v-if="!submitting" name="arrow" :size="16" />
            </button>
          </div>
          <div class="launch-row secondary">
            <p class="hint">{{ agentHint }}</p>
            <label class="cleanup-choice" title="保留已生成的转写稿、笔记与配图；本地导入的视频由你管理">
              <input v-model="options.deleteVideo" type="checkbox" />
              <span>完成后清理下载的视频</span>
            </label>
          </div>
        </section>
        <div v-if="!course" class="learning-notes" aria-label="笔记整理流程">
          <div>
            <AppIcon name="file" :size="20" /><strong>听过，也记得</strong>
            <p>本地转写，留住课堂细节</p>
          </div>
          <div>
            <AppIcon name="spark" :size="20" /><strong>重点，慢慢清晰</strong>
            <p>AI 梳理思路，串起知识脉络</p>
          </div>
          <div>
            <AppIcon name="book" :size="20" /><strong>随时，重新读懂</strong>
            <p>详细与缩略两版，复习有章可循</p>
          </div>
        </div>
        <p v-if="!course" class="new-footnote">每一次回顾，都是新的理解。</p>
      </div>
    </section>
  </div>
  <OptionsDialog ref="optionsDialog" :view-options="course?.view_options ?? []" />
  <dialog ref="overwriteDialog" class="confirm-dialog" @close="overwriteClosed">
    <form method="dialog" class="dialog-header">
      <div>
        <h2>已有整理结果</h2>
        <p>下列 {{ overwrite?.existing.length }} 个课次已有结果，重新整理会覆盖原来的笔记。</p>
      </div>
      <button class="icon-button" value="cancel" aria-label="关闭">×</button>
    </form>
    <div class="confirm-body">
      <ul class="overwrite-list">
        <li v-for="(item, index) in overwrite?.existing ?? []" :key="index">
          <strong>{{ item.title }}</strong>
          <small>{{
            [item.date, item.week ? `第 ${item.week} 周` : "", item.complete ? "笔记完整" : "上次未完成"]
              .filter(Boolean)
              .join(" · ")
          }}</small>
        </li>
      </ul>
      <p class="hint">覆盖会重新生成详细版、缩略版和配图；已下载的视频会复用，不重新下载。</p>
    </div>
    <form method="dialog" class="dialog-footer confirm-actions">
      <button value="cancel" class="quiet-button">取消</button>
      <button
        v-if="(overwrite?.existing.length ?? 0) < (overwrite?.selected.length ?? 0)"
        value="skip"
        class="quiet-button"
      >
        跳过已有
      </button>
      <button value="overwrite" class="primary-button">覆盖并开始</button>
    </form>
  </dialog>
</template>
