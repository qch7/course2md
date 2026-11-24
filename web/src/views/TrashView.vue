<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { ref } from "vue";

import { client, unwrap } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import { loadAll } from "@/stores/jobs";
import { showError, toast, ui } from "@/stores/ui";
import { dateTime } from "@/utils/format";

type Kind = "course" | "job";

const confirmation = ref<InstanceType<typeof ConfirmDialog> | null>(null);
const busy = ref("");
const trash = useQuery({
  queryKey: ["trash"],
  queryFn: () => unwrap(client.GET("/api/v1/trash")),
});

function daysLeft(purgeAt: number): number {
  return Math.max(0, Math.ceil((purgeAt - Date.now() / 1000) / 86400));
}

async function restore(kind: Kind, id: string, title: string): Promise<void> {
  busy.value = id;
  try {
    await unwrap(
      client.POST("/api/v1/trash/{kind}/{item_id}/restore", { params: { path: { kind, item_id: id } } }),
    );
    await Promise.all([loadAll(), trash.refetch()]);
    toast(`已恢复「${title}」`);
  } catch (error) {
    showError(error);
  } finally {
    busy.value = "";
  }
}

async function purge(kind: Kind, id: string, title: string): Promise<void> {
  const removesFiles = trash.data.value?.removes_files ?? false;
  const confirmed = await confirmation.value?.open({
    title: "永久删除",
    message: `确定永久删除「${title}」？`,
    detail: removesFiles
      ? "将立即删除服务器上的笔记、截图与全部历史记录，无法恢复。"
      : "将立即清除课程与任务记录，无法恢复。本地应用不会删除你磁盘上的笔记文件。",
    confirmText: "永久删除",
  });
  if (!confirmed) return;
  busy.value = id;
  try {
    await unwrap(
      client.DELETE("/api/v1/trash/{kind}/{item_id}", { params: { path: { kind, item_id: id } } }),
    );
    await trash.refetch();
    toast("已永久删除");
  } catch (error) {
    showError(error);
  } finally {
    busy.value = "";
  }
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
    <div class="course-heading"><h1>回收站</h1></div>
  </header>
  <div class="workspace-content">
    <div class="page">
      <section class="panel">
        <h3>已删除的课程</h3>
        <p class="hint">
          删除的课程会保留 {{ trash.data.value?.retention_days ?? 30 }} 天，期间可以恢复；到期后自动永久删除。
          {{
            trash.data.value?.removes_files
              ? "永久删除会同时清除服务器上的笔记与截图。"
              : "本地应用只清理记录，不会删除你磁盘上的笔记文件。"
          }}
        </p>
        <p v-if="!(trash.data.value?.items ?? []).length" class="hint">回收站是空的。</p>
        <table v-else class="data-table">
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>删除时间</th>
              <th>剩余</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in trash.data.value?.items ?? []" :key="`${item.kind}:${item.id}`">
              <td>
                {{ item.title }}
                <small v-if="item.kind === 'job' && item.course" class="hint">（{{ item.course }}）</small>
              </td>
              <td>{{ item.kind === "course" ? `课程 · ${item.job_count} 个课次` : "课次" }}</td>
              <td>{{ dateTime(item.deleted_at) }}</td>
              <td>{{ daysLeft(item.purge_at) }} 天</td>
              <td class="row-actions">
                <button
                  class="quiet-button small"
                  :disabled="busy === item.id"
                  @click="restore(item.kind as Kind, item.id, item.title ?? '')"
                >
                  恢复
                </button>
                <button
                  class="quiet-button small danger"
                  :disabled="busy === item.id"
                  @click="purge(item.kind as Kind, item.id, item.title ?? '')"
                >
                  永久删除
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>
  </div>
  <ConfirmDialog ref="confirmation" />
</template>
