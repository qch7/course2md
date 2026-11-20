<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";
import { useRouter } from "vue-router";

import { client, unwrap } from "@/api/client";
import { isAdmin, resetSession, session } from "@/stores/session";
import { showError, toast, ui } from "@/stores/ui";
import { dateTime } from "@/utils/format";

const router = useRouter();
const server = computed(() => session.mode === "server");
const oldPassword = ref("");
const newPassword = ref("");
const tokenName = ref("");
const apiToken = ref("");
const newUser = ref({ username: "", password: "", role: "user" });
const users = useQuery({
  queryKey: ["users"],
  queryFn: () => unwrap(client.GET("/api/v1/users")),
  enabled: computed(() => server.value && isAdmin()),
});

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

async function createUser(): Promise<void> {
  try {
    await unwrap(client.POST("/api/v1/users", { body: newUser.value }));
    toast(`已创建 ${newUser.value.username}`);
    newUser.value = { username: "", password: "", role: "user" };
    users.refetch();
  } catch (error) {
    showError(error);
  }
}

async function setDisabled(id: string, disabled: boolean): Promise<void> {
  try {
    await unwrap(
      client.PATCH("/api/v1/users/{user_id}", { params: { path: { user_id: id } }, body: { disabled } }),
    );
    users.refetch();
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
          <h3>用户</h3>
          <table class="data-table">
            <thead>
              <tr>
                <th>用户名</th>
                <th>角色</th>
                <th>创建时间</th>
                <th>状态</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="user in users.data.value?.users ?? []" :key="user.id">
                <td>{{ user.username }}</td>
                <td>{{ user.role === "admin" ? "管理员" : "用户" }}</td>
                <td>{{ dateTime(user.created_at) }}</td>
                <td>{{ user.disabled ? "已停用" : "正常" }}</td>
                <td>
                  <button
                    v-if="user.id !== session.user?.id"
                    class="quiet-button small"
                    @click="setDisabled(user.id, !user.disabled)"
                  >
                    {{ user.disabled ? "启用" : "停用" }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
          <form class="inline-form" @submit.prevent="createUser">
            <label class="field compact"><span>用户名</span><input v-model="newUser.username" /></label>
            <label class="field compact"
              ><span>初始口令</span
              ><input v-model="newUser.password" type="password" autocomplete="new-password"
            /></label>
            <label class="field compact"
              ><span>角色</span
              ><select v-model="newUser.role">
                <option value="user">用户</option>
                <option value="admin">管理员</option>
              </select></label
            >
            <button class="primary-button">创建用户</button>
          </form>
        </section>
      </template>
    </div>
  </div>
</template>
