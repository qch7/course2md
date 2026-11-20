<script setup lang="ts">
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, ref } from "vue";

import { client, importPlugin, type Plugin, unwrap } from "@/api/client";
import ConfirmDialog from "@/components/ConfirmDialog.vue";
import { session } from "@/stores/session";
import { showError, toast, ui } from "@/stores/ui";

const queryClient = useQueryClient();
const plugins = useQuery({
  queryKey: ["plugins"],
  queryFn: () => unwrap(client.GET("/api/v1/plugins")),
});
const kind = ref<"github" | "file" | "local">("github");
const github = ref("");
const localPath = ref("");
const file = ref<File | null>(null);
const fileInput = ref<HTMLInputElement | null>(null);
const confirmation = ref<InstanceType<typeof ConfirmDialog> | null>(null);
const busy = ref(false);
const operation = ref("");
type PluginView = "cards" | "list";
const viewKey = "course2md.plugins.view";
function storedView(): PluginView {
  try {
    return localStorage.getItem(viewKey) === "list" ? "list" : "cards";
  } catch {
    return "cards";
  }
}
const view = ref<PluginView>(storedView());
function setView(value: PluginView): void {
  view.value = value;
  try {
    localStorage.setItem(viewKey, value);
  } catch {
    // 浏览器禁用存储时仍可切换本次显示方式。
  }
}
const canInstall = computed(() =>
  kind.value === "file" ? !!file.value : !!(kind.value === "github" ? github.value : localPath.value).trim(),
);

function chooseFile(event: Event): void {
  const selected = (event.target as HTMLInputElement).files?.[0] ?? null;
  if (selected && (!/\.(whl|zip|tar\.gz)$/i.test(selected.name) || selected.size > 32 * 1024 * 1024)) {
    file.value = null;
    if (fileInput.value) fileInput.value.value = "";
    showError("请选择不超过 32 MB 的 .whl、.zip 或 .tar.gz 插件安装包");
    return;
  }
  file.value = selected;
}

async function install(): Promise<void> {
  if (busy.value || !canInstall.value) return;
  const source = kind.value === "github" ? github.value.trim() : localPath.value.trim();
  if (
    !(await confirmation.value?.open({
      title: "安装插件",
      message: `安装「${kind.value === "file" ? file.value?.name : source}」？`,
      detail: "只安装你信任的插件。插件将安装到当前 course2md 环境，完成后需要重启应用生效。",
      confirmText: "安装插件",
    }))
  )
    return;
  busy.value = true;
  operation.value = "正在下载并安装插件，请稍候…";
  showError("");
  try {
    const result =
      kind.value === "file"
        ? await importPlugin(file.value!)
        : await unwrap(client.POST("/api/v1/plugins", { body: { source, kind: kind.value } }));
    queryClient.setQueryData(["plugins"], result);
    github.value = "";
    localPath.value = "";
    file.value = null;
    if (fileInput.value) fileInput.value.value = "";
    toast(result.message);
  } catch (error) {
    showError(error);
    await plugins.refetch();
  } finally {
    busy.value = false;
    operation.value = "";
  }
}

async function remove(plugin: Plugin): Promise<void> {
  if (busy.value) return;
  if (
    !(await confirmation.value?.open({
      title: "删除插件",
      message: `确定删除插件「${plugin.label || plugin.name}」？`,
      detail: `将卸载 ${plugin.name}，课程、笔记和历史记录会保留。完成后需要重启应用生效。`,
      confirmText: "删除插件",
    }))
  )
    return;
  busy.value = true;
  operation.value = "正在删除插件，请稍候…";
  showError("");
  try {
    const result = await unwrap(
      client.DELETE("/api/v1/plugins/{name}", { params: { path: { name: plugin.name } } }),
    );
    queryClient.setQueryData(["plugins"], result);
    toast(result.message);
  } catch (error) {
    showError(error);
    await plugins.refetch();
  } finally {
    busy.value = false;
    operation.value = "";
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
    <div class="course-heading"><h1>插件</h1></div>
  </header>
  <div class="workspace-content">
    <div class="page plugins-page">
      <p class="hint">
        来源解析插件负责识别课程链接、列出课次并提供视频地址，转写与笔记整理由 course2md 完成。
      </p>
      <div v-if="plugins.data.value?.restart_required" class="notice plugin-restart" role="status">
        插件已变更，请重启 course2md 后生效。
      </div>
      <p v-if="plugins.error.value" class="notice error-notice" role="alert">
        {{ plugins.error.value.message }}
      </p>
      <section class="plugin-library" aria-labelledby="plugin-library-title">
        <div class="plugin-library-head">
          <h2 id="plugin-library-title">
            已安装插件
            <span v-if="plugins.data.value" class="pill">{{ plugins.data.value.plugins.length }}</span>
          </h2>
          <div class="plugin-toolbar">
            <div class="note-switch plugin-view-switch" role="group" aria-label="插件显示方式">
              <button
                :class="{ active: view === 'cards' }"
                :aria-pressed="view === 'cards'"
                aria-label="卡片视图"
                @click="setView('cards')"
              >
                <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
                  <rect x="3" y="3" width="5" height="5" rx="1" />
                  <rect x="12" y="3" width="5" height="5" rx="1" />
                  <rect x="3" y="12" width="5" height="5" rx="1" />
                  <rect x="12" y="12" width="5" height="5" rx="1" />
                </svg>
                卡片
              </button>
              <button
                :class="{ active: view === 'list' }"
                :aria-pressed="view === 'list'"
                aria-label="列表视图"
                @click="setView('list')"
              >
                <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
                  <path d="M7 5h10M7 10h10M7 15h10M3 5h.01M3 10h.01M3 15h.01" />
                </svg>
                列表
              </button>
            </div>
            <button
              class="quiet-button plugin-refresh"
              :disabled="busy || plugins.isFetching.value"
              @click="plugins.refetch()"
            >
              <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
                <path
                  d="M16 8a6.2 6.2 0 0 0-10.6-2.4L3 8m0-4v4h4M4 12a6.2 6.2 0 0 0 10.6 2.4L17 12m-4 0h4v4"
                />
              </svg>
              刷新
            </button>
          </div>
        </div>
        <p v-if="plugins.isPending.value" class="hint">正在读取插件…</p>
        <div v-else-if="plugins.data.value?.plugins.length === 0" class="plugin-empty">
          <strong>暂无已安装插件</strong>
          <p class="hint">
            {{
              plugins.data.value.can_manage
                ? "从下方导入插件，扩展课程来源。"
                : "安装后将在这里显示可用的来源解析器。"
            }}
          </p>
        </div>
        <ul
          v-else
          class="plugin-list"
          :class="`plugin-list--${view}`"
          aria-label="已安装插件"
          :aria-busy="plugins.isFetching.value"
        >
          <li
            v-for="plugin in plugins.data.value?.plugins ?? []"
            :key="`${plugin.origin}:${plugin.name}`"
            class="plugin-item"
          >
            <div class="plugin-identity">
              <div class="plugin-icon" :class="{ 'plugin-icon--builtin': plugin.origin === 'builtin' }">
                <svg v-if="plugin.origin === 'builtin'" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <rect x="3" y="4" width="18" height="16" rx="3" />
                  <path d="m10 8 6 4-6 4V8Z" />
                </svg>
                <svg v-else viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="m12 3 9 5v8l-9 5-9-5V8l9-5Z" />
                  <path d="m3 8 9 5 9-5M12 13v8M7.5 5.5l9 5" />
                </svg>
              </div>
              <div class="plugin-name">
                <div class="plugin-title">
                  <strong>{{ plugin.label || plugin.name }}</strong>
                  <span v-if="plugin.origin === 'builtin'" class="pill">内置</span>
                  <span v-else-if="plugin.origin === 'env'" class="pill">开发加载</span>
                </div>
                <div class="plugin-meta">
                  <code>{{ plugin.name }}</code>
                  <span v-if="plugin.version" class="plugin-version">v{{ plugin.version }}</span>
                </div>
              </div>
            </div>
            <p v-if="plugin.hint" class="hint plugin-description">{{ plugin.hint }}</p>
            <div class="plugin-footer">
              <div class="plugin-resolvers" aria-label="来源解析器">
                <span v-for="resolver in plugin.resolvers" :key="resolver" class="tag">{{ resolver }}</span>
              </div>
              <button
                v-if="plugin.removable"
                class="quiet-button small plugin-remove"
                :disabled="busy"
                :aria-label="`删除插件 ${plugin.label || plugin.name}`"
                @click="remove(plugin)"
              >
                删除
              </button>
              <span v-else class="plugin-managed">{{
                plugin.origin === "builtin" ? "随应用提供" : plugin.origin === "env" ? "环境变量管理" : "只读"
              }}</span>
            </div>
          </li>
        </ul>
      </section>
      <section v-if="plugins.data.value?.errors.length" class="panel">
        <h3>加载失败</h3>
        <p v-for="problem in plugins.data.value.errors" :key="problem" class="notice error-notice">
          {{ problem }}
        </p>
      </section>
      <section v-if="plugins.data.value?.can_manage" class="panel">
        <h3>安装插件</h3>
        <div class="note-switch plugin-source-switch" role="group" aria-label="安装来源">
          <button
            :class="{ active: kind === 'github' }"
            :aria-pressed="kind === 'github'"
            :disabled="busy"
            @click="kind = 'github'"
          >
            GitHub 链接
          </button>
          <button
            :class="{ active: kind === 'file' }"
            :aria-pressed="kind === 'file'"
            :disabled="busy"
            @click="kind = 'file'"
          >
            本地安装包
          </button>
          <button
            :class="{ active: kind === 'local' }"
            :aria-pressed="kind === 'local'"
            :disabled="busy"
            @click="kind = 'local'"
          >
            本地目录 / 路径
          </button>
        </div>
        <form class="plugin-install-form" @submit.prevent="install">
          <label v-if="kind === 'github'" class="field">
            <span>GitHub 仓库链接</span>
            <input
              v-model="github"
              type="url"
              required
              :disabled="busy"
              placeholder="https://github.com/作者/course2md-插件"
            />
          </label>
          <div v-else-if="kind === 'file'" class="plugin-upload">
            <input
              ref="fileInput"
              type="file"
              hidden
              accept=".whl,.zip,.tar.gz"
              aria-label="插件安装包"
              @change="chooseFile"
            />
            <button type="button" class="quiet-button" :disabled="busy" @click="fileInput?.click()">
              选择插件文件
            </button>
            <span class="hint">{{ file?.name || "未选择文件" }}</span>
          </div>
          <label v-else class="field">
            <span>本机插件项目目录或安装包路径</span>
            <input
              v-model="localPath"
              required
              :disabled="busy"
              placeholder="例如：D:/plugins/course2md-mysite"
            />
          </label>
          <p class="hint">
            支持符合 course2md 插件规范的 Python 项目，以及 .whl、.zip、.tar.gz 安装包；上传文件最大 32 MB。
          </p>
          <div class="plugin-install-actions">
            <p class="hint">插件可以执行代码，只安装你信任的插件。</p>
            <button class="primary-button" type="submit" :disabled="busy || !canInstall">
              {{ busy ? "处理中…" : "安装插件" }}
            </button>
          </div>
        </form>
      </section>
      <section v-else-if="session.mode === 'server'" class="panel">
        <h3>安装插件</h3>
        <p class="hint">
          服务器上的插件由管理员在部署配置（COURSE2MD_PLUGINS）中声明，网页只显示已安装的插件。
        </p>
      </section>
      <p v-if="operation" class="hint" role="status">{{ operation }}</p>
      <p class="hint">
        这里管理当前应用的插件。使用远程计算节点下载课程时，需要在对应节点上另外安装同一插件。
      </p>
    </div>
  </div>
  <ConfirmDialog ref="confirmation" />
</template>
