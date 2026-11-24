<script setup lang="ts">
import { computed, ref } from "vue";
import { useRouter } from "vue-router";

import { client, unwrap } from "@/api/client";
import { isAdmin, resetSession, session } from "@/stores/session";
import { showError, toast, ui } from "@/stores/ui";

const router = useRouter();
const server = computed(() => session.mode === "server");
const oldPassword = ref("");
const newPassword = ref("");
const tokenName = ref("");
const apiToken = ref("");

async function changePassword(): Promise<void> {
  try {
    await unwrap(
      client.POST("/api/v1/auth/password", {
        body: { old_password: oldPassword.value, new_password: newPassword.value },
      }),
    );
    oldPassword.value = newPassword.value = "";
    toast("口令已修改");
  } catch (error) {
    showError(error);
  }
}

async function createToken(): Promise<void> {
  try {
    apiToken.value = (
      await unwrap(client.POST("/api/v1/auth/tokens", { body: { name: tokenName.value || "API 令牌" } }))
    ).token;
  } catch (error) {
    showError(error);
  }
}

async function logout(): Promise<void> {
  resetSession();
  try {
    await unwrap(client.POST("/api/v1/auth/logout"));
  } finally {
    router.replace({ name: "login" });
  }
}
</script>

<template>
  <header class="topbar">
    <button
      class="icon-button mobile-menu"
      aria-label="展开课程列表"
      @click="ui.sidebarOpen = !ui.sidebarOpen"
    >
      ☰
    </button>
    <div class="course-heading"><h1>账号</h1></div>
  </header>
  <div class="workspace-content">
    <div class="page">
      <section v-if="!server" class="panel">
        <h3>本地工作区</h3>
        <p>本地模式不需要登录，服务只监听本机地址。多人共用请按部署说明以服务器形态运行。</p>
      </section>
      <template v-else>
        <section class="panel">
          <h3>
            {{ session.user?.username }}<span class="pill">{{ isAdmin() ? "管理员" : "用户" }}</span>
          </h3>
          <button class="quiet-button" @click="logout">退出登录</button>
        </section>
        <section class="panel">
          <h3>修改口令</h3>
          <form class="inline-form" @submit.prevent="changePassword">
            <label class="field compact"
              ><span>原口令</span
              ><input v-model="oldPassword" type="password" autocomplete="current-password"
            /></label>
            <label class="field compact"
              ><span>新口令（至少 8 位）</span
              ><input v-model="newPassword" type="password" autocomplete="new-password"
            /></label>
            <button class="primary-button">修改</button>
          </form>
        </section>
        <section class="panel">
          <h3>API 令牌</h3>
          <p class="hint">供脚本或其它客户端以 Authorization: Bearer 访问接口，令牌只显示一次。</p>
          <div class="inline-form">
            <label class="field compact"
              ><span>用途</span><input v-model="tokenName" placeholder="例如：同步脚本"
            /></label>
            <button class="primary-button" @click="createToken">生成</button>
          </div>
          <code v-if="apiToken" class="copyable">{{ apiToken }}</code>
        </section>
        <section v-if="isAdmin()" class="panel">
          <h3>管理</h3>
          <p>创建用户、修改角色、停用账号与重置口令，请前往<RouterLink to="/admin">管理页</RouterLink>。</p>
        </section>
      </template>
    </div>
  </div>
</template>
