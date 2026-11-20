<script setup lang="ts">
import { computed, ref } from "vue";

import { options, session } from "@/stores/session";
import { agentName } from "@/utils/format";

defineProps<{ viewOptions: { id: number | string; label: string; default?: boolean }[] }>();

const dialog = ref<HTMLDialogElement | null>(null);
const formats = ["srt", "vtt", "txt", "md", "json"];
const usesCli = computed(() => options.agent === "dsh" || options.agent === "codex");
const known = computed(
  () =>
    session.config?.agent_models?.[options.agent] ?? {
      default_provider: "",
      default_model: "",
      providers: {} as Record<string, string[]>,
    },
);
const choice = computed(() => {
  if (!options.agentChoices[options.agent]) options.agentChoices[options.agent] = { provider: "", model: "" };
  return options.agentChoices[options.agent];
});
const selectors = computed(() => session.config?.frame_selectors ?? []);
const selectorHint = computed(
  () => selectors.value.find((item) => item.name === options.frameSelector)?.description ?? "",
);
const providerOptions = computed(() => Object.keys(known.value.providers ?? {}));
const modelOptions = computed<string[]>(() => {
  const providers: Record<string, string[]> = known.value.providers ?? {};
  const provider = choice.value.provider || known.value.default_provider || "";
  return [...new Set(provider ? (providers[provider] ?? []) : Object.values(providers).flat())];
});

function toggleFormat(format: string, checked: boolean): void {
  options.formats = checked
    ? [...options.formats, format]
    : options.formats.filter((item) => item !== format);
}

function toggleView(id: number, checked: boolean): void {
  options.views = checked ? [...options.views, id] : options.views.filter((item) => item !== id);
}

defineExpose({ open: () => dialog.value?.showModal() });
</script>

<template>
  <dialog ref="dialog" class="settings-dialog">
    <form method="dialog" class="dialog-header">
      <div>
        <h2>整理选项</h2>
        <p>用于下一次课程整理</p>
      </div>
      <button class="icon-button" aria-label="关闭设置">×</button>
    </form>
    <div class="settings-body">
      <label v-if="!session.config?.server" class="field"
        ><span>输出目录</span><input v-model="options.outDir" type="text"
      /></label>
      <div class="settings-grid">
        <label class="field"><span>转写模型</span><input v-model="options.model" /></label>
        <label class="field"
          ><span>转写设备</span
          ><select v-model="options.device">
            <option value="cuda">GPU · CUDA</option>
            <option value="cpu">CPU</option>
          </select></label
        >
      </div>
      <fieldset>
        <legend>输出格式</legend>
        <div class="check-row">
          <label v-for="format in formats" :key="format"
            ><input
              type="checkbox"
              :checked="options.formats.includes(format)"
              @change="toggleFormat(format, ($event.target as HTMLInputElement).checked)"
            />{{ format === "md" ? "Markdown" : format.toUpperCase() }}</label
          >
        </div>
      </fieldset>
      <fieldset v-if="viewOptions.length">
        <legend>机位</legend>
        <div class="check-row">
          <label v-for="view in viewOptions" :key="view.id"
            ><input
              type="checkbox"
              :checked="options.views.includes(Number(view.id))"
              @change="toggleView(Number(view.id), ($event.target as HTMLInputElement).checked)"
            />{{ view.label }}</label
          >
        </div>
        <p class="hint">由当前来源的解析器提供，未勾选时处理全部默认机位。</p>
      </fieldset>
      <div class="setting-checks">
        <label><input v-model="options.refine" type="checkbox" /><span>生成详细版与缩略版</span></label>
        <label
          ><input v-model="options.proofread" type="checkbox" :disabled="!options.refine" /><span
            >校对转写稿<small>纠正同音字与术语</small></span
          ></label
        >
        <label
          ><input v-model="options.frames" type="checkbox" /><span
            >结合课堂画面<small>读取 PPT、板书与例题截图</small></span
          ></label
        >
        <label><input v-model="options.detail" type="checkbox" /><span>优先展示详细版</span></label>
        <label><input v-model="options.keepAudio" type="checkbox" /><span>保留中间音频 WAV</span></label>
      </div>
      <label v-if="selectors.length > 1" class="field"
        ><span>画面筛选策略</span
        ><select v-model="options.frameSelector" :disabled="!options.frames">
          <option v-for="item in selectors" :key="item.name" :value="item.name">
            {{ item.label }}{{ item.builtin ? "" : " · 插件" }}
          </option>
        </select></label
      >
      <p v-if="selectors.length > 1 && selectorHint" class="hint">{{ selectorHint }}</p>
      <div class="settings-divider"></div>
      <label class="field"
        ><span>大模型接口</span
        ><input
          v-model="options.llmBase"
          type="text"
          :disabled="session.config?.server && session.user?.role !== 'admin'"
      /></label>
      <label class="field"><span>内置整理模型</span><input v-model="options.llmModel" /></label>
      <p class="hint">
        {{
          session.config?.has_llm_key
            ? "API 已配置，可用于校对、长课证据提取与内置整理。"
            : "校对、长课证据提取与内置整理需要配置 API Key。"
        }}
      </p>
      <template v-if="usesCli">
        <div class="settings-divider"></div>
        <div class="settings-grid">
          <label class="field"
            ><span>{{ agentName(options.agent) }} 提供商</span
            ><input
              v-model="choice.provider"
              list="agent-provider-options"
              autocomplete="off"
              spellcheck="false"
              :placeholder="known.default_provider ? `默认：${known.default_provider}` : '沿用 CLI 配置'"
          /></label>
          <label class="field"
            ><span>Agent 模型</span
            ><input
              v-model="choice.model"
              list="agent-model-options"
              autocomplete="off"
              spellcheck="false"
              :placeholder="known.default_model ? `默认：${known.default_model}` : '沿用 CLI 配置'"
          /></label>
        </div>
        <datalist id="agent-provider-options">
          <option v-for="item in providerOptions" :key="item" :value="item"></option>
        </datalist>
        <datalist id="agent-model-options">
          <option v-for="item in modelOptions" :key="item" :value="item"></option>
        </datalist>
        <p class="hint">
          {{
            options.agent === "codex"
              ? "留空沿用 ~/.codex/config.toml；提供商须已在其中的 model_providers 登记。"
              : "留空沿用 DeepSeek Harness 的默认模型；提供商须已在 DSH 设置中登记。"
          }}
        </p>
      </template>
    </div>
    <form method="dialog" class="dialog-footer"><button class="primary-button">完成</button></form>
  </dialog>
</template>
