<script setup lang="ts">
import { computed, ref } from "vue";

import { summarizeCapabilities } from "@/utils/capabilities";

const props = withDefaults(
  defineProps<{
    capabilities?: readonly string[];
    /** 没有任何能力时显示的说明 */
    empty?: string;
    /** 折叠时最多显示几个标签 */
    limit?: number;
  }>(),
  { capabilities: () => [], empty: "", limit: 7 },
);

const open = ref(false);
const all = computed(() => summarizeCapabilities(props.capabilities));
const shown = computed(() => (open.value ? all.value : all.value.slice(0, props.limit)));
const hidden = computed(() => all.value.length - shown.value.length);
</script>

<template>
  <div class="capabilities">
    <span v-for="tag in shown" :key="tag.label" class="tag" :title="tag.title">{{ tag.label }}</span>
    <button v-if="hidden > 0" type="button" class="tag tag-more" @click="open = true">+{{ hidden }}</button>
    <button v-else-if="open && all.length > limit" type="button" class="tag tag-more" @click="open = false">
      收起
    </button>
    <span v-if="!all.length && empty" class="muted">{{ empty }}</span>
  </div>
</template>
