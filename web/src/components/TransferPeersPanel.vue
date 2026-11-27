<script setup lang="ts">
import { reactive, ref } from "vue";

import { client, type TransferPeer, unwrap } from "@/api/client";
import type ConfirmDialog from "@/components/ConfirmDialog.vue";
import { showError, toast } from "@/stores/ui";

// 对端 Hub 面板：保存、编辑、删除对端 Hub。
// 列表由父组件的查询提供；改动后发出 changed 让父组件刷新，并用事件把“推送/拉取到此 Hub”交给父组件。
const props = defineProps<{
  peers: TransferPeer[];
  // 页面共用的确认对话框，避免页面里出现两个确认框实例
  confirm: InstanceType<typeof ConfirmDialog> | null;
}>();
const emit = defineEmits<{
  changed: [];
  saved: [peerId: string];
  deleted: [peerId: string];
  usePush: [peer: TransferPeer];
  usePull: [peer: TransferPeer];
}>();

// ------------------------------------------------------------------ 对端 Hub 管理
const showAddPeer = ref(false);
const savingPeer = ref(false);
const editingPeerId = ref<string | null>(null);
const newPeer = reactive({ name: "", hub_url: "", token: "" });
const editPeer = reactive({ name: "", hub_url: "", token: "" });

async function addPeer(): Promise<void> {
  if (!newPeer.hub_url.trim()) return showError("请先填写对端 Hub 地址");
  savingPeer.value = true;
  try {
    const data = await unwrap(
      client.POST("/api/v1/transfer/peers", {
        body: {
          name: newPeer.name.trim(),
          hub_url: newPeer.hub_url.trim(),
          token: newPeer.token.trim(),
        },
      }),
    );
    newPeer.name = "";
    newPeer.hub_url = "";
    newPeer.token = "";
    showAddPeer.value = false;
    emit("changed");
    emit("saved", data.peer.id);
    toast("已保存对端 Hub");
  } catch (error) {
    showError(error);
  } finally {
    savingPeer.value = false;
  }
}

function startEditPeer(peer: TransferPeer): void {
  editingPeerId.value = peer.id;
  editPeer.name = peer.name ?? "";
  editPeer.hub_url = peer.hub_url ?? "";
  editPeer.token = "";
}

async function saveEditPeer(peerId: string): Promise<void> {
  savingPeer.value = true;
  try {
    const body: Record<string, string> = {
      name: editPeer.name.trim(),
      hub_url: editPeer.hub_url.trim(),
    };
    if (editPeer.token.trim()) body.token = editPeer.token.trim();
    await unwrap(
      client.PATCH("/api/v1/transfer/peers/{peer_id}", {
        params: { path: { peer_id: peerId } },
        body,
      }),
    );
    editingPeerId.value = null;
    emit("changed");
    toast("已更新对端 Hub");
  } catch (error) {
    showError(error);
  } finally {
    savingPeer.value = false;
  }
}

async function deletePeer(peer: TransferPeer): Promise<void> {
  const ok = await props.confirm?.open({
    title: "删除对端 Hub",
    message: `确定移除「${peer.name || peer.hub_url}」？`,
    detail: "只移除当前保存的连接信息，不影响已传输的课程与笔记。",
    confirmText: "移除",
  });
  if (!ok) return;
  try {
    await unwrap(
      client.DELETE("/api/v1/transfer/peers/{peer_id}", { params: { path: { peer_id: peer.id } } }),
    );
    if (editingPeerId.value === peer.id) editingPeerId.value = null;
    emit("changed");
    emit("deleted", peer.id);
    toast("已移除对端 Hub");
  } catch (error) {
    showError(error);
  }
}
</script>

<template>
  <section class="panel">
    <div class="panel-header">
      <div>
        <h3>常用对端 Hub</h3>
        <p class="hint">
          在多个本地应用或服务器 Hub
          间互传课程笔记、转写、课件截图、对话上下文与版本历史；传输时自动剥离登录凭证与原始视频。
        </p>
      </div>
      <button class="primary-button" @click="showAddPeer = !showAddPeer">
        {{ showAddPeer ? "收起" : "添加对端 Hub" }}
      </button>
    </div>

    <form v-if="showAddPeer" class="hub-add-form" @submit.prevent="addPeer">
      <div class="hub-form-grid">
        <label class="field compact">
          <span>对端 Hub 地址</span>
          <input v-model="newPeer.hub_url" placeholder="例如 https://course.example.com" required />
        </label>
        <label class="field compact">
          <span>名称（可选）</span>
          <input v-model="newPeer.name" placeholder="例如 实验室服务器 / 我的台式机" />
        </label>
      </div>
      <label class="field compact">
        <span>API 令牌（对端为服务器形态时必填，在对端「账号」页生成；本地应用可留空）</span>
        <input v-model="newPeer.token" type="password" autocomplete="off" placeholder="留空或粘贴 API 令牌" />
      </label>
      <div class="inline-actions">
        <button type="submit" class="primary-button" :disabled="savingPeer">保存对端</button>
        <button type="button" class="quiet-button" @click="showAddPeer = false">取消</button>
      </div>
    </form>

    <div v-if="peers.length" class="transfer-peers">
      <div v-for="peer in peers" :key="peer.id" class="transfer-peer-card">
        <template v-if="editingPeerId === peer.id">
          <div class="hub-edit-box">
            <div class="hub-form-grid">
              <label class="field compact">
                <span>名称</span>
                <input v-model="editPeer.name" />
              </label>
              <label class="field compact">
                <span>Hub 地址</span>
                <input v-model="editPeer.hub_url" />
              </label>
            </div>
            <label class="field compact">
              <span>新 API 令牌（留空则沿用已保存令牌）</span>
              <input v-model="editPeer.token" type="password" autocomplete="off" />
            </label>
            <div class="inline-actions">
              <button class="primary-button" :disabled="savingPeer" @click="saveEditPeer(peer.id)">
                保存
              </button>
              <button class="quiet-button" @click="editingPeerId = null">取消</button>
            </div>
          </div>
        </template>
        <template v-else>
          <div class="peer-main">
            <strong>{{ peer.name || peer.hub_url }}</strong>
            <span class="pill">{{ peer.has_token ? `令牌 ${peer.token_hint}` : "无令牌" }}</span>
            <small class="peer-url">{{ peer.hub_url }}</small>
          </div>
          <div class="peer-actions">
            <button class="quiet-button" @click="emit('usePush', peer)">推送到此 Hub</button>
            <button class="quiet-button" @click="emit('usePull', peer)">从此 Hub 拉取</button>
            <button class="quiet-button" @click="startEditPeer(peer)">编辑</button>
            <button class="quiet-button danger" @click="deletePeer(peer)">删除</button>
          </div>
        </template>
      </div>
    </div>
    <p v-else-if="!showAddPeer" class="hint">尚未保存对端 Hub，可直接添加常用 Hub，或在下方临时填写地址。</p>
  </section>
</template>
