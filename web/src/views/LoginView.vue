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
const setupToken = ref("");
const error = ref("");
const busy = ref(false);

type Action = "login" | "register" | "setup";

async function submit(action: Action): Promise<void> {
  error.value = "";
  busy.value = true;
  try {
    const body = { username: username.value, password: password.value };
    if (action === "setup") {
      await unwrap(
        client.POST("/api/v1/auth/setup", { body: { ...body, setup_token: setupToken.value.trim() } }),
      );
    } else {
      await unwrap(
        action === "register"
          ? client.POST("/api/v1/auth/register", { body })
          : client.POST("/api/v1/auth/login", { body }),
      );
    }
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
    <form class="login-card" @submit.prevent="submit(session.needsSetup ? 'setup' : 'login')">
      <div class="brand"><BrandMark /><span>course2md</span></div>
      <div class="login-intro">
        <h1>{{ session.needsSetup ? "初始化服务。" : "欢迎回来。" }}</h1>
        <p>让每一堂课，都成为自己的知识。</p>
      </div>
      <template v-if="session.needsSetup">
        <p class="hint">
          服务还没有任何账号。初始化令牌已打印在服务的启动日志里（Docker 部署：<code
            >docker compose logs hub</code
          >）， 填入后创建管理员账号。
        </p>
        <label class="sr-only" for="setup-token">初始化令牌</label>
        <input
          id="setup-token"
          v-model="setupToken"
          autocomplete="off"
          spellcheck="false"
          placeholder="初始化令牌（c2s_…）"
          required
        />
      </template>
      <label class="sr-only" for="username">用户名</label>
      <input id="username" v-model="username" autocomplete="username" placeholder="用户名" required />
      <label class="sr-only" for="password">口令</label>
      <input
        id="password"
        v-model="password"
        type="password"
        :autocomplete="session.needsSetup ? 'new-password' : 'current-password'"
        placeholder="口令（至少 8 位）"
        required
      />
      <div class="login-actions">
        <button class="primary-button" type="submit" :disabled="busy">
          {{ busy ? "请稍候…" : session.needsSetup ? "创建管理员" : "登录" }}
        </button>
        <button
          v-if="!session.needsSetup && session.registration === 'open'"
          class="quiet-button"
          type="button"
          :disabled="busy"
          @click="submit('register')"
        >
          注册
        </button>
      </div>
      <p class="error-text">{{ error }}</p>
    </form>
  </div>
</template>
