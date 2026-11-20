<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref, watch } from "vue";

import { client, type Job, unwrap } from "@/api/client";
import { absorb, isBusy, watchJob } from "@/stores/jobs";
import { session } from "@/stores/session";
import { showError, toast } from "@/stores/ui";
import { agentName, dateTime, duration, stageNames, statusNames } from "@/utils/format";

const extraStageNames: Record<string, string> = { chat: "对话", cleanup: "清理视频" };

const props = defineProps<{ job: Job }>();
const busy = computed(() => isBusy(props.job));
const tasks = useQuery({
  queryKey: computed(() => ["tasks", props.job.id]),
  queryFn: () =>
    unwrap(client.GET("/api/v1/jobs/{job_id}/tasks", { params: { path: { job_id: props.job.id } } })),
  refetchInterval: () => (busy.value ? 2000 : false),
});
watch(
  () => [props.job.status, props.job.revision],
  () => tasks.refetch(),
);

const fromStage = ref("organize");
const agent = ref(props.job.agent === "builtin" ? "none" : (props.job.agent ?? "none"));
const llmModel = ref("");
const agents = computed(() => Object.keys(session.config?.agents ?? {}));
const submitting = ref(false);

async function rerun(): Promise<void> {
  submitting.value = true;
  try {
    const data = await unwrap(
      client.POST("/api/v1/jobs/{job_id}/rerun", {
        params: { path: { job_id: props.job.id } },
        body: { from_stage: fromStage.value, agent: agent.value, llm_model: llmModel.value || null },
      }),
    );
    absorb(data.job);
    watchJob(props.job.id);
    toast(`已从「${stageNames[fromStage.value]}」重新开始，之前的阶段复用已有结果`);
    tasks.refetch();
  } catch (error) {
    showError(error);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="panel-scroll">
    <section class="panel">
      <h3>阶段执行记录</h3>
      <p class="hint">每个阶段由具备相应能力的计算节点执行，结果按内容保存，可单独重跑。</p>
      <table class="data-table">
        <thead>
          <tr>
            <th>阶段</th>
            <th>状态</th>
            <th>节点</th>
            <th>开始</th>
            <th>耗时</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="task in tasks.data.value?.tasks ?? []" :key="task.id">
            <td>{{ stageNames[task.stage ?? ""] ?? extraStageNames[task.stage ?? ""] ?? task.stage }}</td>
            <td>
              <span class="pill" :class="task.status">{{
                statusNames[task.status ?? ""] ?? task.status
              }}</span>
            </td>
            <td>{{ task.worker_id || "—" }}</td>
            <td>{{ dateTime(task.started_at) }}</td>
            <td>{{ duration(task.started_at, task.finished_at) }}</td>
            <td class="wrap">
              {{ task.error || task.progress?.message || (task.warnings ?? []).join("；") }}
            </td>
          </tr>
          <tr v-if="!tasks.data.value?.tasks?.length">
            <td colspan="6" class="empty">还没有阶段记录（旧任务或进程内执行的任务没有记录）。</td>
          </tr>
        </tbody>
      </table>
    </section>
    <section class="panel">
      <h3>从某个阶段重新运行</h3>
      <p class="hint">之前阶段的输入与设置不变时直接复用结果，例如只换模型重新整理，不必重新下载与转写。</p>
      <div class="inline-form">
        <label class="field compact">
          <span>起始阶段</span>
          <select v-model="fromStage">
            <option v-for="(label, name) in stageNames" :key="name" :value="name">{{ label }}</option>
          </select>
        </label>
        <label class="field compact">
          <span>整理方式</span>
          <select v-model="agent">
            <option value="none">内置模型</option>
            <option v-for="name in agents" :key="name" :value="name">{{ agentName(name) }}</option>
          </select>
        </label>
        <label class="field compact">
          <span>内置模型名（可选）</span>
          <input v-model="llmModel" :placeholder="session.config?.llm_model" />
        </label>
        <button class="primary-button" :disabled="busy || submitting" @click="rerun">重新运行</button>
      </div>
    </section>
  </div>
</template>
