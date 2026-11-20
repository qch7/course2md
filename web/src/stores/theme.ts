import { ref, watchEffect } from "vue";

export type ThemeChoice = "system" | "light" | "dark";

const KEY = "course2md.theme";
const media = typeof matchMedia === "function" ? matchMedia("(prefers-color-scheme: dark)") : null;

function stored(): ThemeChoice {
  try {
    const value = localStorage.getItem(KEY);
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}

export const theme = ref<ThemeChoice>(stored());
const systemDark = ref(media?.matches ?? false);
media?.addEventListener("change", (event) => (systemDark.value = event.matches));

export const themeLabels: Record<ThemeChoice, string> = { system: "跟随系统", light: "浅色", dark: "深色" };

/** 依次切换：跟随系统 → 浅色 → 深色。 */
export function cycleTheme(): void {
  theme.value = theme.value === "system" ? "light" : theme.value === "light" ? "dark" : "system";
  try {
    if (theme.value === "system") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, theme.value);
  } catch {
    /* 隐私模式忽略 */
  }
}

watchEffect(() => {
  const dark = theme.value === "dark" || (theme.value === "system" && systemDark.value);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
});
