<script setup lang="ts">
import { computed } from "vue";

export type MeshKind = "hub" | "local" | "remote" | "external";
export type MeshStatus = "on" | "busy" | "warn" | "err" | "off";

export interface MeshNode {
  id: string;
  kind: MeshKind;
  label: string;
  /** 图中节点下方的小字，例如能力数量 */
  sub?: string;
  status: MeshStatus;
}

const props = defineProps<{
  hub: MeshNode;
  local: MeshNode[];
  remote: MeshNode[];
  external: MeshNode[];
  /** 哪些分组末尾显示「＋」入口 */
  addable: Partial<Record<Exclude<MeshKind, "hub">, string>>;
  selectedKey?: string;
}>();

const emit = defineEmits<{
  select: [node: MeshNode];
  add: [kind: Exclude<MeshKind, "hub">];
}>();

// 版心约 800px，画布尺寸贴近它，文字缩放后才不会过小
const WIDTH = 800;
const HEIGHT = 400;
const CX = WIDTH / 2;
const CY = 190;
const RX = 262;
const RY = 128;

/** 三个分组各占一个扇区：本机节点在左，连入的节点在下，外部 Hub 在右。 */
const SECTORS: Record<Exclude<MeshKind, "hub">, { center: number; maxSpan: number; step: number }> = {
  local: { center: 180, maxSpan: 150, step: 36 },
  remote: { center: 90, maxSpan: 130, step: 40 },
  external: { center: 0, maxSpan: 150, step: 36 },
};

interface Placed {
  key: string;
  node: MeshNode | null;
  kind: Exclude<MeshKind, "hub">;
  x: number;
  y: number;
}

function place(kind: Exclude<MeshKind, "hub">, nodes: MeshNode[]): Placed[] {
  const addLabel = props.addable[kind];
  const items: (MeshNode | null)[] = addLabel ? [...nodes, null] : [...nodes];
  const sector = SECTORS[kind];
  const span = Math.min(sector.maxSpan, sector.step * Math.max(0, items.length - 1));
  return items.map((node, index) => {
    const offset = items.length === 1 ? 0 : -span / 2 + (span * index) / (items.length - 1);
    const angle = ((sector.center + offset) * Math.PI) / 180;
    return {
      key: node ? `${node.kind}:${node.id}` : `add:${kind}`,
      node,
      kind,
      x: CX + RX * Math.cos(angle),
      y: CY + RY * Math.sin(angle),
    };
  });
}

const placed = computed<Placed[]>(() => [
  ...place("local", props.local),
  ...place("remote", props.remote),
  ...place("external", props.external),
]);

/** 按近似宽度截断：中文约 13px，西文约 7px，避免两侧标签压到相邻节点。 */
function shorten(text: string, maxWidth = 100): string {
  let width = 0;
  for (let index = 0; index < text.length; index += 1) {
    width += text.charCodeAt(index) > 255 ? 13 : 7;
    if (width > maxWidth) return `${text.slice(0, Math.max(1, index - 1))}…`;
  }
  return text;
}

function labelSide(kind: Exclude<MeshKind, "hub">): "left" | "right" | "below" {
  return kind === "local" ? "left" : kind === "external" ? "right" : "below";
}

function activate(item: Placed): void {
  if (item.node) emit("select", item.node);
  else emit("add", item.kind);
}

const KIND_NAMES: Record<MeshKind, string> = {
  hub: "本机 Hub",
  local: "本机节点",
  remote: "连入节点",
  external: "外部 Hub",
};

const STATUS_NAMES: Record<MeshStatus, string> = {
  on: "在线",
  busy: "执行中",
  warn: "连接中",
  err: "异常",
  off: "离线",
};

function ariaLabel(item: Placed): string {
  if (!item.node) return props.addable[item.kind] ?? "";
  return `${KIND_NAMES[item.node.kind]} ${item.node.label}，${STATUS_NAMES[item.node.status]}，点击查看详情`;
}

const hubKey = computed(() => `hub:${props.hub.id}`);
</script>

<template>
  <svg
    class="mesh-topology"
    :viewBox="`0 0 ${WIDTH} ${HEIGHT}`"
    role="group"
    aria-label="计算服务网状拓扑"
    preserveAspectRatio="xMidYMid meet"
  >
    <g class="mesh-edges">
      <line
        v-for="item in placed"
        :key="`edge-${item.key}`"
        class="mesh-edge"
        :class="[
          `kind-${item.kind}`,
          item.node ? `status-${item.node.status}` : 'status-add',
          { selected: item.node && selectedKey === item.key },
        ]"
        :x1="CX"
        :y1="CY"
        :x2="item.x"
        :y2="item.y"
      />
    </g>

    <g
      v-for="item in placed"
      :key="item.key"
      class="mesh-vertex"
      :class="[
        `kind-${item.kind}`,
        item.node ? `status-${item.node.status}` : 'status-add',
        { selected: item.node && selectedKey === item.key },
      ]"
      role="button"
      tabindex="0"
      :aria-label="ariaLabel(item)"
      :transform="`translate(${item.x} ${item.y})`"
      @click="activate(item)"
      @keydown.enter.prevent="activate(item)"
      @keydown.space.prevent="activate(item)"
    >
      <title v-if="item.node">{{ item.node.label }}</title>
      <template v-if="item.node">
        <rect
          v-if="item.kind === 'local'"
          class="mesh-shape"
          x="-24"
          y="-20"
          width="48"
          height="40"
          rx="10"
        />
        <circle v-else-if="item.kind === 'remote'" class="mesh-shape" r="22" />
        <rect
          v-else
          class="mesh-shape"
          x="-18"
          y="-18"
          width="36"
          height="36"
          rx="5"
          transform="rotate(45)"
        />
        <circle class="mesh-dot" cx="17" cy="-17" r="5.5" />
      </template>
      <template v-else>
        <circle class="mesh-shape mesh-add-shape" r="17" />
        <path class="mesh-plus" d="M-6 0H6M0 -6V6" />
      </template>

      <text
        class="mesh-label"
        :class="{ 'mesh-label-add': !item.node }"
        :x="labelSide(item.kind) === 'left' ? -34 : labelSide(item.kind) === 'right' ? 34 : 0"
        :y="labelSide(item.kind) === 'below' ? 44 : item.node?.sub ? -2 : 4"
        :text-anchor="
          labelSide(item.kind) === 'left' ? 'end' : labelSide(item.kind) === 'right' ? 'start' : 'middle'
        "
      >
        {{ item.node ? shorten(item.node.label) : (addable[item.kind] ?? "") }}
      </text>
      <text
        v-if="item.node?.sub"
        class="mesh-sub"
        :x="labelSide(item.kind) === 'left' ? -34 : labelSide(item.kind) === 'right' ? 34 : 0"
        :y="labelSide(item.kind) === 'below' ? 60 : 14"
        :text-anchor="
          labelSide(item.kind) === 'left' ? 'end' : labelSide(item.kind) === 'right' ? 'start' : 'middle'
        "
      >
        {{ item.node.sub }}
      </text>
    </g>

    <g
      class="mesh-vertex mesh-hub kind-hub"
      :class="{ selected: selectedKey === hubKey }"
      role="button"
      tabindex="0"
      :aria-label="`${KIND_NAMES.hub} ${hub.label}，点击查看详情`"
      :transform="`translate(${CX} ${CY})`"
      @click="emit('select', hub)"
      @keydown.enter.prevent="emit('select', hub)"
      @keydown.space.prevent="emit('select', hub)"
    >
      <circle class="mesh-halo" r="62" />
      <circle class="mesh-shape" r="46" />
      <text class="mesh-hub-title" y="-3" text-anchor="middle">{{ hub.label }}</text>
      <text v-if="hub.sub" class="mesh-hub-sub" y="16" text-anchor="middle">{{ hub.sub }}</text>
    </g>
  </svg>
</template>
