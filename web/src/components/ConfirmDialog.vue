<script setup lang="ts">
import { onBeforeUnmount, ref, useId } from "vue";

interface Confirmation {
  title: string;
  message: string;
  detail?: string;
  confirmText?: string;
}

const dialog = ref<HTMLDialogElement | null>(null);
const content = ref<Confirmation>({ title: "", message: "" });
const titleId = useId();
const descriptionId = useId();
let resolveConfirmation: ((confirmed: boolean) => void) | null = null;

function open(confirmation: Confirmation): Promise<boolean> {
  if (!dialog.value || resolveConfirmation) return Promise.resolve(false);
  content.value = confirmation;
  dialog.value.returnValue = "";
  return new Promise((resolve) => {
    resolveConfirmation = resolve;
    dialog.value!.showModal();
  });
}

function settle(confirmed: boolean): void {
  const resolve = resolveConfirmation;
  resolveConfirmation = null;
  resolve?.(confirmed);
}

onBeforeUnmount(() => settle(false));
defineExpose({ open });
</script>

<template>
  <dialog
    ref="dialog"
    class="confirm-dialog"
    :aria-labelledby="titleId"
    :aria-describedby="descriptionId"
    @close="settle(dialog?.returnValue === 'confirm')"
    @cancel.prevent="dialog?.close('cancel')"
  >
    <div class="dialog-header">
      <h2 :id="titleId">{{ content.title }}</h2>
      <button type="button" class="icon-button" aria-label="关闭" @click="dialog?.close('cancel')">×</button>
    </div>
    <div :id="descriptionId" class="confirm-body">
      <p>{{ content.message }}</p>
      <p v-if="content.detail" class="hint">{{ content.detail }}</p>
    </div>
    <div class="dialog-footer">
      <button type="button" class="quiet-button" autofocus @click="dialog?.close('cancel')">取消</button>
      <button type="button" class="primary-button" @click="dialog?.close('confirm')">
        {{ content.confirmText || "确定" }}
      </button>
    </div>
  </dialog>
</template>
