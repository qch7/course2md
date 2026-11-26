<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, reactive, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { client, type Course, type Job, unwrap } from "@/api/client";
import AppIcon from "@/components/AppIcon.vue";
import BrandMark from "@/components/BrandMark.vue";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import { dropJob, isBusy, store } from "@/stores/jobs";
import { isAdmin, session } from "@/stores/session";
import { cycleTheme, theme, themeLabels } from "@/stores/theme";
import { showError, toast, ui } from "@/stores/ui";
import { agentName, sessionLabel, statusNames } from "@/utils/format";

const route = useRoute();
const router = useRouter();
const search = ref("");
const confirmation = ref<InstanceType<typeof ConfirmDialog> | null>(null);

const hubLinks = useQuery({
  queryKey: ["hub-links"],
  queryFn: () => unwrap(client.GET("/api/v1/hub-links")),
  enabled: computed(() => session.mode === "local" || isAdmin()),
  refetchInterval: 10000,
});

const meshStatus = computed(() => {
  const links = hubLinks.data.value?.links ?? [];
  if (!links.length) return null;
  const total = links.length;
  const online = links.filter((item) => item.status === "online" || item.status === "busy").length;
  if (links.some((item) => item.status === "auth_failed")) {
    return { cls: "err", badge: "令牌失效", caption: "外部 Hub 令牌失效" };
  }
  if (links.some((item) => item.status === "busy")) {
    return { cls: "busy", badge: `${online}/${total}`, caption: "正在为外部 Hub 执行任务" };
  }
  if (online > 0) {
    return {
      cls: "on",
      badge: `${online}/${total}`,
      caption: total === 1 ? "已连 1 个外部 Hub" : `已连 ${online}/${total} 个外部 Hub`,
    };
  }
  if (links.some((item) => item.status === "connecting" || item.status === "reconnecting")) {
    return { cls: "warn", badge: "重连中", caption: "正在连接外部 Hub" };
  }
  return { cls: "", badge: `0/${total}`, caption: "外部 Hub 未连接" };
});

function loadFolders(): Record<string, boolean> {
  try {
    const stored = JSON.parse(localStorage.getItem("course2md.folders") || "{}");
    return stored && typeof stored === "object" ? stored : {};
  } catch {
    return {};
  }
}
const folders = reactive(loadFolders());

const selectedJob = computed(() => (route.name === "job" ? String(route.params.jobId) : ""));
const selectedCourse = computed(() =>
  route.name === "course"
    ? String(route.params.courseId)
    : (store.jobs.get(selectedJob.value)?.course_id ?? ""),
);

interface Group {
  key: string;
  course: Pick<Course, "id" | "title"> & Partial<Course>;
  jobs: Job[];
}

const groups = computed<Group[]>(() => {
  const map = new Map<string, Group>();
  [...store.courses.values()]
    .sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0))
    .forEach((course) => map.set(course.id, { key: course.id, course, jobs: [] }));
  [...store.jobs.values()]
    .sort((a, b) => (b.started_at ?? 0) - (a.started_at ?? 0))
    .forEach((job) => {
      const key = job.course_id || `legacy:${job.course || "未分类"}`;
      if (!map.has(key)) map.set(key, { key, course: { id: key, title: job.course || "未分类" }, jobs: [] });
      map.get(key)!.jobs.push(job);
    });
  const filter = search.value.trim().toLowerCase();
  return [...map.values()].filter((group) => {
    if (!filter) return true;
    const text = [
      group.course.title,
      group.course.source,
      ...(group.course.sessions ?? []).map((item) => item.title),
      ...group.jobs.map((job) => job.title),
    ]
      .join(" ")
      .toLowerCase();
    return text.includes(filter);
  });
});

function expanded(group: Group): boolean {
  if (group.key in folders) return folders[group.key];
  return group.key === selectedCourse.value || !selectedJob.value;
}

function toggle(group: Group): void {
  folders[group.key] = !expanded(group);
  try {
    localStorage.setItem("course2md.folders", JSON.stringify(folders));
  } catch {
    /* 隐私模式忽略 */
  }
}

function openGroup(group: Group): void {
  ui.sidebarOpen = false;
  if (store.courses.has(group.key)) router.push({ name: "course", params: { courseId: group.key } });
  else if (group.jobs[0]) router.push({ name: "job", params: { jobId: group.jobs[0].id } });
}

function go(to: Parameters<typeof router.push>[0]): void {
  ui.sidebarOpen = false;
  router.push(to);
}

/** 移入回收站（逻辑删除）：进行中的任务会被取消，30 天内可在回收站恢复。 */
async function removeGroup(group: Group): Promise<void> {
  const busy = group.jobs.some(isBusy);
  const confirmed = await confirmation.value?.open({
    title: "删除课程",
    message: `确定删除「${group.course.title}」？`,
    detail: busy
      ? "其中有正在整理的课次，删除会先取消它们。课程会移入回收站，30 天内可以恢复。"
      : "课程会移入回收站，30 天内可以恢复，之后才会永久删除。",
    confirmText: "移入回收站",
  });
  if (!confirmed) return;
  try {
    if (store.courses.has(group.key)) {
      await unwrap(
        client.DELETE("/api/v1/courses/{course_id}", { params: { path: { course_id: group.key } } }),
      );
      store.courses.delete(group.key);
    } else {
      // 没有课程记录的旧任务逐个删除
      for (const job of group.jobs) {
        await unwrap(client.DELETE("/api/v1/jobs/{job_id}", { params: { path: { job_id: job.id } } }));
      }
    }
    const viewing =
      selectedCourse.value === group.key || group.jobs.some((job) => job.id === selectedJob.value);
    group.jobs.forEach((job) => dropJob(job.id));
    if (viewing) await router.replace("/");
    toast("已移入回收站，30 天内可以恢复");
  } catch (error) {
    showError(error);
  }
}
</script>

<template>
  <aside class="sidebar" :class="{ open: ui.sidebarOpen }">
    <a class="brand" href="/" @click.prevent="go('/')">
      <BrandMark />
      <span>course2md<span class="brand-caption">课程笔记</span></span>
    </a>
    <button class="new-course" :class="{ active: route.name === 'new' }" @click="go('/')">
      <AppIcon name="plus" :size="18" />
      新建课程
      <AppIcon name="book" :size="16" class="new-course-book" />
    </button>
    <label class="search-box">
      <AppIcon name="search" :size="16" />
      <span class="sr-only">搜索课程</span>
      <input v-model="search" placeholder="搜索课程" autocomplete="off" />
    </label>
    <div class="sidebar-heading">
      我的课程 <span>{{ groups.length || "" }}</span>
    </div>
    <nav class="course-list" aria-label="课程列表">
      <section v-for="group in groups" :key="group.key" class="course-group">
        <div class="course-group-heading">
          <button
            type="button"
            class="folder-toggle"
            :aria-expanded="expanded(group)"
            :aria-label="`${expanded(group) ? '收起' : '展开'}${group.course.title}课次`"
            @click="toggle(group)"
          >
            <AppIcon class="folder-chevron" name="chevron" :size="13" />
          </button>
          <button
            type="button"
            class="course-folder"
            :class="{
              selected: selectedCourse === group.key && !selectedJob,
              busy: group.jobs.some(isBusy),
              failed: group.jobs.some((job) => job.status === 'failed'),
            }"
            :title="`${group.course.title} · 下载其他节次`"
            @click="openGroup(group)"
          >
            <span class="folder-icon" aria-hidden="true"></span>
            <span class="folder-name">{{ group.course.title }}</span>
            <span class="folder-count">{{ group.course.sessions?.length || group.jobs.length }}</span>
          </button>
          <button
            type="button"
            class="folder-delete"
            :aria-label="`删除课程${group.course.title}`"
            title="移入回收站"
            @click="removeGroup(group)"
          >
            <AppIcon name="trash" :size="14" />
          </button>
        </div>
        <div
          class="course-sessions-collapse"
          :class="{ expanded: expanded(group) }"
          :inert="!expanded(group)"
        >
          <div class="course-sessions">
            <button
              v-for="job in group.jobs"
              :key="job.id"
              class="course-item"
              :class="{
                selected: selectedJob === job.id,
                busy: isBusy(job),
                failed: job.status === 'failed',
              }"
              :aria-current="selectedJob === job.id ? 'page' : 'false'"
              :title="job.title"
              @click="go({ name: 'job', params: { jobId: job.id } })"
            >
              <span class="name">
                <span class="name-text">{{ sessionLabel(job) }}</span>
                <b v-if="job.week" class="week-badge">第 {{ job.week }} 周</b>
              </span>
              <span class="info">
                <i class="status-dot"></i>{{ agentName(job.agent) }} ·
                {{ statusNames[job.status ?? ""] ?? job.status }}
              </span>
            </button>
          </div>
        </div>
      </section>
      <div v-if="!groups.length" class="history-empty">
        <AppIcon :name="search ? 'search' : 'book'" :size="24" />
        <p>{{ search ? "没有匹配的课程" : "你的知识，从这里积累" }}</p>
        <small>{{ search ? "试试其他关键词" : "解析课程后，随时回来接着读" }}</small>
      </div>
    </nav>
    <nav class="sidebar-nav" aria-label="管理">
      <RouterLink to="/trash" @click="ui.sidebarOpen = false"><AppIcon name="trash" />回收站</RouterLink>
      <RouterLink to="/workers" @click="ui.sidebarOpen = false">
        <AppIcon name="server" />计算服务
        <span v-if="meshStatus" class="nav-badge" :class="meshStatus.cls">{{ meshStatus.badge }}</span>
      </RouterLink>
      <RouterLink to="/plugins" @click="ui.sidebarOpen = false"><AppIcon name="puzzle" />插件</RouterLink>
      <RouterLink to="/account" @click="ui.sidebarOpen = false"><AppIcon name="user" />账号</RouterLink>
      <RouterLink v-if="session.mode === 'server' && isAdmin()" to="/admin" @click="ui.sidebarOpen = false"
        ><AppIcon name="settings" />管理</RouterLink
      >
    </nav>
    <div class="sidebar-footer">
      <div class="workspace-identity">
        <span class="workspace-avatar">{{
          session.mode === "server" ? session.user?.username?.slice(0, 1).toUpperCase() : "C"
        }}</span>
        <span
          ><strong>{{ session.mode === "server" ? session.user?.username : "本地工作区" }}</strong
          ><small
            ><i class="online-dot" :class="session.mode === 'local' && meshStatus ? meshStatus.cls : ''"></i
            >{{
              session.mode === "server"
                ? "个人学习空间"
                : meshStatus
                  ? meshStatus.caption
                  : "在自己的节奏里学习"
            }}</small
          ></span
        >
      </div>
      <button
        class="icon-button"
        :title="`外观：${themeLabels[theme]}（点击切换）`"
        :aria-label="`切换外观，当前${themeLabels[theme]}`"
        @click="cycleTheme"
      >
        <AppIcon :name="theme === 'dark' ? 'moon' : theme === 'light' ? 'sun' : 'system'" :size="18" />
      </button>
    </div>
    <ConfirmDialog ref="confirmation" />
  </aside>
</template>
