<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import Sidebar from "./components/Sidebar.vue";
import { loadAll } from "./stores/jobs";
import { loadConfig, loadStatus, loadUser, resetSession, session } from "./stores/session";
import { showError, ui } from "./stores/ui";

const route = useRoute();
const router = useRouter();
const ready = ref(false);

async function start(): Promise<void> {
  resetSession();
  ready.value = false;
  try {
    await loadStatus();
    const user = await loadUser();
    if (!user) {
      ready.value = true;
      if (route.name !== "login") await router.replace({ name: "login", query: { next: route.fullPath } });
      return;
    }
    const config = await loadConfig();
    await loadAll();
    if (config.startup_warnings?.length) showError(config.startup_warnings.join("\n"));
  } catch (error) {
    showError(error);
  }
  ready.value = true;
}

window.addEventListener("course2md:unauthorized", () => {
  if (session.mode === "server" && route.name !== "login") {
    resetSession();
    router.replace({ name: "login", query: { next: route.fullPath } });
  }
});

onMounted(start);
defineExpose({ start });
</script>

<template>
  <RouterView v-if="route.meta.bare" @signed-in="start" />
  <div v-else-if="ready && session.user" class="app-shell" @keydown.esc="ui.sidebarOpen = false">
    <button
      v-if="ui.sidebarOpen"
      class="sidebar-scrim"
      aria-label="收起课程列表"
      @click="ui.sidebarOpen = false"
    ></button>
    <Sidebar />
    <main class="workspace">
      <Transition name="notice">
        <div v-if="ui.error" class="notice error-notice global-error" role="alert">
          <span>{{ ui.error }}</span>
          <button class="icon-button" aria-label="关闭提示" @click="ui.error = ''">×</button>
        </div>
      </Transition>
      <RouterView v-slot="{ Component }">
        <Transition name="page" mode="out-in">
          <div :key="route.path" class="route-view"><component :is="Component" /></div>
        </Transition>
      </RouterView>
    </main>
    <Transition name="toast"
      ><div v-if="ui.toast" class="toast" role="status">{{ ui.toast }}</div></Transition
    >
  </div>
  <div v-else class="loading-screen">{{ ui.error || "正在加载…" }}</div>
</template>
