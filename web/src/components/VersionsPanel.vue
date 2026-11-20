<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import MarkdownIt from "markdown-it";
import { computed, ref, watch } from "vue";

import { client, type Job, unwrap } from "@/api/client";
import { absorb, isBusy } from "@/stores/jobs";
import { showError, toast } from "@/stores/ui";
import { dateTime } from "@/utils/format";

import ConfirmDialog from "./ConfirmDialog.vue";

const props = defineProps<{ job: Job }>();
const confirmation = ref<InstanceType<typeof ConfirmDialog> | null>(null);
const markdown = new MarkdownIt({ html: false, linkify: true });
const versions = useQuery({
  queryKey: computed(() => ["versions", props.job.id]),
  queryFn: () =>
    unwrap(client.GET("/api/v1/jobs/{job_id}/versions", { params: { path: { job_id: props.job.id } } })),
});
watch(
  () => props.job.revision,
  () => versions.refetch(),
);

const current = ref<number | null>(null);
const kind = ref<"detailed" | "brief">("detailed");
const text = ref("");
const html = computed(() => (text.value ? markdown.render(text.value) : ""));

async function open(version: number): Promise<void> {
  current.value = version;
  text.value = "正在读取…";
  try {
    const data = await unwrap(
      client.GET("/api/v1/jobs/{job_id}/versions/{version}", {
        params: { path: { job_id: props.job.id, version }, query: { kind: kind.value } },
      }),
    );
    text.value = data.text ?? "";
  } catch (error) {
    text.value = "";
    showError(error);
  }
}

watch(kind, () => current.value && open(current.value));

async function restore(version: number): Promise<void> {
  if (
    !(await confirmation.value?.open({
      title: "恢复笔记版本",
      message: `把笔记恢复为第 ${version} 版？`,
      detail: "当前内容会作为历史版本保留。",
      confirmText: "恢复版本",
    }))
  )
    return;
  try {
    const data = await unwrap(
      client.POST("/api/v1/jobs/{job_id}/versions/{version}/restore", {
        params: { path: { job_id: props.job.id, version } },
      }),
    );
    absorb(data.job);
    toast(`已恢复到第 ${version} 版`);
    versions.refetch();
  } catch (error) {
    showError(error);
  }
}
</script>

<template>
  <div class="versions-view">
    <aside class="version-list">
      <button
        v-for="item in versions.data.value?.versions ?? []"
        :key="item.version"
        class="version-item"
        :class="{ active: current === item.version }"
        @click="open(item.version)"
      >
        <strong>第 {{ item.version }} 版</strong>
        <span>{{ item.label }}</span>
        <small>{{ dateTime(item.created_at) }}{{ item.model ? ` · ${item.model}` : "" }}</small>
      </button>
      <p v-if="!versions.data.value?.versions?.length" class="hint">
        整理完成或对话修改笔记后会在这里保留版本。
      </p>
    </aside>
    <section class="version-content">
      <div v-if="current" class="notes-toolbar">
        <div class="note-switch">
          <button :class="{ active: kind === 'detailed' }" @click="kind = 'detailed'">详细版</button>
          <button :class="{ active: kind === 'brief' }" @click="kind = 'brief'">缩略版</button>
        </div>
        <button class="quiet-button" :disabled="isBusy(job)" @click="restore(current)">恢复为此版本</button>
      </div>
      <div v-if="current" class="note-content markdown-preview" v-html="html"></div>
      <p v-else class="hint">选择左侧的版本查看内容。</p>
    </section>
    <ConfirmDialog ref="confirmation" />
  </div>
</template>
