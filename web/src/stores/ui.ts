import { reactive } from "vue";

export const ui = reactive({
  error: "",
  toast: "",
  sidebarOpen: false,
});

let timer: ReturnType<typeof setTimeout> | undefined;

export function toast(text: string): void {
  ui.toast = text;
  clearTimeout(timer);
  timer = setTimeout(() => (ui.toast = ""), 3500);
}

export function showError(error: unknown): void {
  ui.error = error instanceof Error ? error.message : error ? String(error) : "";
}
