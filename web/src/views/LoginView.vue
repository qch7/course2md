<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { client, unwrap } from "@/api/client";
import BrandMark from "@/components/BrandMark.vue";
import { loadStatus, session } from "@/stores/session";

const emit = defineEmits<{ "signed-in": [] }>();
const route = useRoute();
const router = useRouter();
const username = ref("");
const password = ref("");
const error = ref("");
const busy = ref(false);

async function submit(register: boolean): Promise<void> {
  error.value = "";
  busy.value = true;
  try {
    const body = { username: username.value, password: password.value };
    await unwrap(
      register ? client.POST("/api/v1/auth/register", { body }) : client.POST("/api/v1/auth/login", { body }),
    );
    const next =
      typeof route.query.next === "string" && route.query.next.startsWith("/") ? route.query.next : "/";
    await router.replace(next);
    emit("signed-in");
  } catch (failure) {
    error.value = (failure as Error).message;
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  await loadStatus();
  if (session.mode === "local") {
    await router.replace("/");
    emit("signed-in");
  }
});
</script>

<template>
  <div class="login-screen">
    <form class="login-card" @submit.prevent="submit(!session.hasUsers)">
      <div class="brand"><BrandMark /><span>course2md</span></div>
      <div class="login-intro">
        <h1>{{ session.hasUsers ? "欢迎回来。" : "学习，从这里开始。" }}</h1>
        <p>让每一堂课，都成为自己的知识。</p>
      </div>
      <p v-if="!session.hasUsers" class="hint">这是第一个账号，注册后成为管理员。</p>
      <label class="sr-only" for="username">用户名</label>
      <input id="username" v-model="username" autocomplete="username" placeholder="用户名" required />
      <label class="sr-only" for="password">口令</label>
      <input
        id="password"
        v-model="password"
        type="password"
        :autocomplete="session.hasUsers ? 'current-password' : 'new-password'"
        placeholder="口令（至少 8 位）"
        required
      />
      <div class="login-actions">
        <button class="primary-button" type="submit" :disabled="busy">
          {{ busy ? "请稍候…" : session.hasUsers ? "登录" : "注册" }}
        </button>
        <button
          v-if="session.hasUsers && session.registration === 'open'"
          class="quiet-button"
          type="button"
          :disabled="busy"
          @click="submit(true)"
        >
          注册
        </button>
      </div>
      <p class="error-text">{{ error }}</p>
    </form>
  </div>
</template>
