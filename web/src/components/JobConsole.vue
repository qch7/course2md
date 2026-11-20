<script setup lang="ts">
import { nextTick, ref, watch } from "vue";

import type { JobEvent } from "@/api/client";

const props = defineProps<{ events: JobEvent[]; compact?: boolean }>();
const box = ref<HTMLElement | null>(null);
const labels: Record<string, string> = {
  assistant: "文本输出",
  tool: "工具",
  status: "状态",
  session: "会话",
  error: "错误",
  log: "CLI",
};

function time(seconds: number | undefined): string {
  return seconds ? new Date(seconds * 1000).toLocaleTimeString("zh-CN", { hour12: false }) : "";
}

watch(
  () => props.events.length,
  async () => {
    const element = box.value;
    if (!element) return;
    const follow = element.scrollHeight - element.scrollTop - element.clientHeight < 100;
    await nextTick();
    if (follow) element.scrollTop = element.scrollHeight;
  },
);

defineExpose({
  scrollToEnd: () => {
    if (box.value) box.value.scrollTop = box.value.scrollHeight;
  },
});
</script>

<template>
  <div ref="box" class="console" :class="compact ? 'compact-console' : 'full-console'">
    <template v-if="events.length">
      <div v-for="event in events" :key="event.seq" class="console-row" :class="event.kind">
        <div class="event-head">
          <b>{{ labels[event.kind ?? ""] ?? "输出" }}</b>{{ time(event.time) }}
        </div>
        <pre>{{ event.text }}{{ event.truncated ? "\n[本条展示已截断，完整输出保存在 agent.log]" : "" }}</pre>
      </div>
    </template>
    <template v-else>等待 Agent 输出…</template>
  </div>
</template>
