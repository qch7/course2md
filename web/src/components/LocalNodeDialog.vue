<script setup lang="ts">
import { computed, reactive, ref, useId } from "vue";

import type { LocalNode } from "@/api/client";
import { type CapabilityItem, groupCapabilities } from "@/utils/capabilities";

export interface LocalNodeForm {
  name: string;
  auto: boolean;
  capabilities: string[];
  enabled: boolean;
}

const props = defineProps<{ available: string[]; busy?: boolean }>();
const emit = defineEmits<{ save: [id: string | null, form: LocalNodeForm] }>();

const dialog = ref<HTMLDialogElement | null>(null);
const titleId = useId();
const editingId = ref<string | null>(null);
const builtin = ref(false);
const form = reactive<LocalNodeForm>({ name: "", auto: false, capabilities: [], enabled: true });

const groups = computed(() => groupCapabilities(props.available));
const invalid = computed(() => !form.auto && form.capabilities.length === 0);

function openCreate(): void {
  editingId.value = null;
  builtin.value = false;
  form.name = "";
  form.auto = false;
  // 新节点默认勾选全部能力，用户再去掉不想承担的部分，比从零勾选省事
  form.capabilities = [...props.available];
  form.enabled = true;
  dialog.value?.showModal();
}

function openEdit(node: LocalNode): void {
  editingId.value = node.id;
  builtin.value = Boolean(node.builtin);
  form.name = node.name ?? "";
  form.auto = Boolean(node.auto);
  form.capabilities = [...(node.auto ? props.available : (node.selected ?? []))];
  form.enabled = Boolean(node.enabled);
  dialog.value?.showModal();
}

function close(): void {
  dialog.value?.close();
}

/** 一个勾选项可能代表多个能力名（解析器及其带版本的声明），要一起增减。 */
function isChecked(item: CapabilityItem): boolean {
  return item.names.some((name) => form.capabilities.includes(name));
}

function toggle(item: CapabilityItem, checked: boolean): void {
  toggleGroup(item.names, checked);
}

function toggleGroup(names: string[], checked: boolean): void {
  const next = new Set(form.capabilities);
  for (const name of names) {
    if (checked) next.add(name);
    else next.delete(name);
  }
  form.capabilities = [...next].sort();
}

function submit(): void {
  if (invalid.value) return;
  emit("save", editingId.value, { ...form, capabilities: [...form.capabilities] });
}

defineExpose({ openCreate, openEdit, close });
</script>

<template>
  <dialog ref="dialog" class="local-node-dialog" :aria-labelledby="titleId" @cancel.prevent="close">
    <form @submit.prevent="submit">
      <div class="dialog-header">
        <div>
          <h2 :id="titleId">{{ editingId ? "编辑本机节点" : "新建本机节点" }}</h2>
          <p>
            本机节点与 Hub
            同进程运行，用来把转写、画面、整理等能力分开承担；所有本机节点共享显存，一次只执行一个阶段。
          </p>
        </div>
        <button type="button" class="icon-button" aria-label="关闭" @click="close">×</button>
      </div>

      <div class="node-dialog-body">
        <label class="field">
          <span>节点名称</span>
          <input v-model="form.name" maxlength="60" placeholder="例如：只做转写" />
        </label>

        <fieldset class="capability-mode">
          <legend>承担的能力</legend>
          <label class="check-inline">
            <input v-model="form.auto" type="radio" :value="true" name="capability-mode" />
            自动：随本机检测结果走，装了新插件或模型后自动获得对应能力
          </label>
          <label class="check-inline">
            <input v-model="form.auto" type="radio" :value="false" name="capability-mode" />
            自定义：只承担勾选的能力
          </label>
        </fieldset>

        <div v-if="!form.auto" class="capability-picker">
          <p v-if="!groups.length" class="muted">本机没有检测到可承担的能力。</p>
          <fieldset v-for="group in groups" :key="group.key" class="capability-pick-group">
            <legend>
              {{ group.title }}
              <button
                type="button"
                class="text-button"
                @click="
                  toggleGroup(
                    group.items.flatMap((item) => item.names),
                    !group.items.every(isChecked),
                  )
                "
              >
                {{ group.items.every(isChecked) ? "全不选" : "全选" }}
              </button>
            </legend>
            <div class="check-row">
              <label v-for="item in group.items" :key="item.name" :title="item.hint || item.name">
                <input
                  type="checkbox"
                  :checked="isChecked(item)"
                  @change="toggle(item, ($event.target as HTMLInputElement).checked)"
                />
                {{ item.label }}
              </label>
            </div>
          </fieldset>
          <p v-if="invalid" class="link-error">请至少选择一项能力，或改为自动。</p>
        </div>

        <label class="check-inline">
          <input v-model="form.enabled" type="checkbox" />
          启用（停用后节点离线，不再领取任务）
        </label>
        <p v-if="builtin" class="hint">内置节点不能删除，只能修改能力或停用。</p>
      </div>

      <div class="dialog-footer">
        <button type="button" class="quiet-button" @click="close">取消</button>
        <button type="submit" class="primary-button" :disabled="busy || invalid">
          {{ busy ? "保存中…" : editingId ? "保存" : "创建节点" }}
        </button>
      </div>
    </form>
  </dialog>
</template>
