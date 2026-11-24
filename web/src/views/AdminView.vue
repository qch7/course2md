<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref } from "vue";

import { client, unwrap } from "@/api/client";
import { isAdmin, session } from "@/stores/session";
import { showError, toast, ui } from "@/stores/ui";
import { dateTime } from "@/utils/format";

const allowed = computed(() => session.mode === "server" && isAdmin());
const newUser = ref({ username: "", password: "", role: "user" });
const reset = ref({ userId: "", password: "" });
const users = useQuery({
  queryKey: ["users"],
  queryFn: () => unwrap(client.GET("/api/v1/users")),
  enabled: allowed,
});

const others = computed(() => (users.data.value?.users ?? []).filter((user) => user.id !== session.user?.id));

async function patch(
  id: string,
  body: { disabled?: boolean; role?: string; password?: string },
): Promise<boolean> {
  try {
    await unwrap(client.PATCH("/api/v1/users/{user_id}", { params: { path: { user_id: id } }, body }));
    users.refetch();
    return true;
  } catch (error) {
    showError(error);
    users.refetch(); // 失败时让下拉框回到服务器上的真实取值
    return false;
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

async function resetPassword(): Promise<void> {
  if (await patch(reset.value.userId, { password: reset.value.password })) {
    toast("口令已重置，该用户需要用新口令重新登录");
    reset.value = { userId: "", password: "" };
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
    <div class="course-heading"><h1>管理</h1></div>
  </header>
  <div class="workspace-content">
    <div class="page">
      <section v-if="!allowed" class="panel">
        <h3>需要管理员权限</h3>
        <p>管理页只在服务器形态下对管理员开放。本地应用不需要账号管理。</p>
      </section>
      <template v-else>
        <section class="panel">
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
                <td>
                  <span v-if="user.id === session.user?.id" class="pill">管理员（你）</span>
                  <select
                    v-else
                    :value="user.role"
                    :aria-label="`${user.username} 的角色`"
                    @change="patch(user.id, { role: ($event.target as HTMLSelectElement).value })"
                  >
                    <option value="user">用户</option>
                    <option value="admin">管理员</option>
                  </select>
                </td>
                <td>{{ dateTime(user.created_at) }}</td>
                <td>{{ user.disabled ? "已停用" : "正常" }}</td>
                <td>
                  <button
                    v-if="user.id !== session.user?.id"
                    class="quiet-button small"
                    @click="patch(user.id, { disabled: !user.disabled })"
                  >
                    {{ user.disabled ? "启用" : "停用" }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </section>
        <section class="panel">
          <h3>创建用户</h3>
          <form class="inline-form" @submit.prevent="createUser">
            <label class="field compact"
              ><span>用户名</span><input v-model="newUser.username" required
            /></label>
            <label class="field compact"
              ><span>初始口令（至少 8 位）</span
              ><input v-model="newUser.password" type="password" autocomplete="new-password" required
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
          <p class="hint">
            {{
              session.registration === "open"
                ? "当前开放自助注册，任何人都可以在登录页创建普通账号。"
                : "当前未开放自助注册，账号只能由管理员创建。开放注册请设置环境变量 COURSE2MD_REGISTRATION=open 后重启。"
            }}
          </p>
        </section>
        <section class="panel">
          <h3>重置口令</h3>
          <p class="hint">用于同学忘记口令时。重置后该用户的登录会话与 API 令牌立即失效。</p>
          <form class="inline-form" @submit.prevent="resetPassword">
            <label class="field compact"
              ><span>用户</span
              ><select v-model="reset.userId" required>
                <option value="" disabled>选择用户</option>
                <option v-for="user in others" :key="user.id" :value="user.id">{{ user.username }}</option>
              </select></label
            >
            <label class="field compact"
              ><span>新口令（至少 8 位）</span
              ><input v-model="reset.password" type="password" autocomplete="new-password" required
            /></label>
            <button class="primary-button">重置</button>
          </form>
        </section>
      </template>
    </div>
  </div>
</template>
