<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

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
const MARGIN_X = 30;
const MARGIN_Y = 28;
// 指针移动超过这个距离才算拖动，否则仍是点击，避免轻微抖动吞掉点击
const DRAG_THRESHOLD = 4;

/**
 * 弹簧物理。做法参考 d3-force：拖动时固定被拖节点、让其余节点继续受力，松手后各力把图带回平衡。
 * 与 d3 不同的是这里用「力 → 速度 → 位置」且阻尼偏小，节点会来回弹几下再停，而不是慢慢滑到位。
 * 单位是画布像素与固定 60Hz 步长，不依赖屏幕刷新率。
 */
const STEP_MS = 1000 / 60;
const K_LINK = 0.05; // Hub 与节点之间的弹簧：拖一个节点会通过 Hub 带动其余节点
const K_HOME = 0.02; // 回到各自扇区位置的弱拉力，保证松手后布局恢复
const K_COLLIDE = 0.12; // 节点互相挤压时的推力
const REPEL = 500; // 节点之间的远距离斥力，让邻居主动让路
const DAMPING = 0.93; // 每步保留的速度比例，越大越弹
const DAMPING_CALM = 0.55; // 偏好减少动画时用重阻尼，几乎不回弹
const HUB_MASS = 2.5;
const MAX_SPEED = 28;
const REST_SPEED = 0.04;

type Point = { x: number; y: number };

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
  /** 没有外力时停靠的位置 */
  home: Point;
}

interface Body {
  key: string;
  x: number;
  y: number;
  vx: number;
  vy: number;
  /** 上一步的位置，被拖动时据此估算速度，松手后带着这个速度继续飞 */
  px: number;
  py: number;
  hx: number;
  hy: number;
  rest: number;
  radius: number;
  mass: number;
  pinned: boolean;
  isHub: boolean;
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
      home: { x: CX + RX * Math.cos(angle), y: CY + RY * Math.sin(angle) },
    };
  });
}

const placed = computed<Placed[]>(() => [
  ...place("local", props.local),
  ...place("remote", props.remote),
  ...place("external", props.external),
]);

const hubKey = computed(() => `hub:${props.hub.id}`);

// ------------------------------------------------------------ 模拟状态
const bodies = new Map<string, Body>();
/** 每帧写回的渲染坐标；对象整体替换，Vue 只在帧间更新一次 */
const positions = ref<Record<string, Point>>({});
const dragKey = ref("");
let frame = 0;
let lastTime = 0;
let carry = 0;
let primed = false;

const canAnimate = typeof requestAnimationFrame === "function";
const calm = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;

function pointOf(key: string, fallback: Point): Point {
  return positions.value[key] ?? fallback;
}

const hubPoint = computed<Point>(() => pointOf(hubKey.value, { x: CX, y: CY }));

function makeBody(key: string, home: Point, isHub: boolean, start: Point): Body {
  return {
    key,
    x: start.x,
    y: start.y,
    vx: 0,
    vy: 0,
    px: start.x,
    py: start.y,
    hx: home.x,
    hy: home.y,
    rest: 0,
    radius: isHub ? 62 : 34,
    mass: isHub ? HUB_MASS : 1,
    pinned: false,
    isHub,
  };
}

/** 节点增减或排布变化时对齐物理体：新节点从 Hub 弹出，已有节点保留当前位置与速度。 */
function syncBodies(): void {
  const hubHome = { x: CX, y: CY };
  const wanted = new Map<string, { home: Point; isHub: boolean }>([
    [hubKey.value, { home: hubHome, isHub: true }],
  ]);
  for (const item of placed.value) wanted.set(item.key, { home: item.home, isHub: false });

  for (const key of [...bodies.keys()]) if (!wanted.has(key)) bodies.delete(key);
  for (const [key, spec] of wanted) {
    let body = bodies.get(key);
    if (!body) {
      // 首次渲染直接放在原位，之后新增的才从 Hub 弹出，避免每次进页面都满屏乱飞
      body = makeBody(key, spec.home, spec.isHub, primed && !spec.isHub ? hubHome : spec.home);
      bodies.set(key, body);
    }
    body.hx = spec.home.x;
    body.hy = spec.home.y;
    body.rest = spec.isHub ? 0 : Math.hypot(spec.home.x - hubHome.x, spec.home.y - hubHome.y);
  }
  primed = true;
  publish();
  wake();
}

function publish(): void {
  const next: Record<string, Point> = {};
  for (const body of bodies.values()) next[body.key] = { x: body.x, y: body.y };
  positions.value = next;
}

function tick(): void {
  const list = [...bodies.values()];
  const hub = bodies.get(hubKey.value);
  const fx = new Map<Body, number>();
  const fy = new Map<Body, number>();
  for (const body of list) {
    fx.set(body, (body.hx - body.x) * K_HOME);
    fy.set(body, (body.hy - body.y) * K_HOME);
  }
  const push = (body: Body, x: number, y: number): void => {
    fx.set(body, (fx.get(body) ?? 0) + x);
    fy.set(body, (fy.get(body) ?? 0) + y);
  };

  if (hub) {
    for (const body of list) {
      if (body === hub) continue;
      const dx = body.x - hub.x;
      const dy = body.y - hub.y;
      const dist = Math.hypot(dx, dy) || 0.001;
      const force = K_LINK * (dist - body.rest);
      push(body, (-force * dx) / dist, (-force * dy) / dist);
      push(hub, (force * dx) / dist, (force * dy) / dist);
    }
  }

  for (let i = 0; i < list.length; i += 1) {
    for (let j = i + 1; j < list.length; j += 1) {
      const a = list[i]!;
      const b = list[j]!;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.hypot(dx, dy) || 0.001;
      const nx = dx / dist;
      const ny = dy / dist;
      let force = 0;
      const touching = a.radius + b.radius;
      if (dist < touching) force += (touching - dist) * K_COLLIDE;
      // Hub 与节点之间已经有弹簧，斥力只在节点之间起作用
      if (!a.isHub && !b.isHub) force += REPEL / Math.max(dist * dist, 900);
      if (!force) continue;
      push(a, -force * nx, -force * ny);
      push(b, force * nx, force * ny);
    }
  }

  const damping = calm ? DAMPING_CALM : DAMPING;
  for (const body of list) {
    if (body.pinned) {
      // 被拖动的节点由指针决定位置；记录这一步的位移作为松手时的初速度
      body.vx = body.x - body.px;
      body.vy = body.y - body.py;
      body.px = body.x;
      body.py = body.y;
      continue;
    }
    body.vx = Math.max(
      -MAX_SPEED,
      Math.min(MAX_SPEED, (body.vx + (fx.get(body) ?? 0) / body.mass) * damping),
    );
    body.vy = Math.max(
      -MAX_SPEED,
      Math.min(MAX_SPEED, (body.vy + (fy.get(body) ?? 0) / body.mass) * damping),
    );
    body.x += body.vx;
    body.y += body.vy;
    body.px = body.x;
    body.py = body.y;
    // 撞到画布边缘时反弹并损失一半速度，不会飞出视野
    if (body.x < MARGIN_X || body.x > WIDTH - MARGIN_X) {
      body.x = Math.min(WIDTH - MARGIN_X, Math.max(MARGIN_X, body.x));
      body.vx *= -0.5;
    }
    if (body.y < MARGIN_Y || body.y > HEIGHT - MARGIN_Y) {
      body.y = Math.min(HEIGHT - MARGIN_Y, Math.max(MARGIN_Y, body.y));
      body.vy *= -0.5;
    }
  }
}

function settled(): boolean {
  for (const body of bodies.values()) {
    if (body.pinned) return false;
    if (Math.hypot(body.vx, body.vy) > REST_SPEED) return false;
  }
  return true;
}

function loop(time: number): void {
  frame = 0;
  carry += Math.min(time - lastTime, 50);
  lastTime = time;
  let steps = 0;
  while (carry >= STEP_MS && steps < 4) {
    tick();
    carry -= STEP_MS;
    steps += 1;
  }
  publish();
  // 这一帧没凑够一个步长（高刷新率屏，或首帧的浮点误差）时速度还是初始值 0，不能据此判定已静止
  if (steps === 0 || !settled() || dragKey.value) wake();
  else lastTime = 0;
}

function wake(): void {
  if (!canAnimate || frame) return;
  frame = requestAnimationFrame((time) => {
    if (!lastTime) lastTime = time - STEP_MS;
    loop(time);
  });
}

watch([placed, hubKey], syncBodies, { immediate: true });

onBeforeUnmount(() => {
  if (frame && typeof cancelAnimationFrame === "function") cancelAnimationFrame(frame);
  frame = 0;
});

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

// ------------------------------------------------------------ 拖动
const svg = ref<SVGSVGElement | null>(null);
let drag: {
  key: string;
  start: Point;
  origin: Point;
  scale: number;
  moved: boolean;
  pointerId: number;
} | null = null;
let suppressClick = "";

/** 屏幕像素换算到画布坐标的比例；SVG 按 meet 缩放，横纵比例相同，jsdom 里取不到尺寸时按 1 处理。 */
function pixelScale(): number {
  const rect = svg.value?.getBoundingClientRect();
  if (!rect || !rect.width || !rect.height) return 1;
  return Math.max(WIDTH / rect.width, HEIGHT / rect.height);
}

function startDrag(event: PointerEvent, key: string): void {
  const body = bodies.get(key);
  if (event.button !== 0 || !body) return;
  drag = {
    key,
    start: { x: event.clientX, y: event.clientY },
    origin: { x: body.x, y: body.y },
    scale: pixelScale(),
    moved: false,
    pointerId: event.pointerId,
  };
  (event.currentTarget as Element).setPointerCapture?.(event.pointerId);
}

function moveDrag(event: PointerEvent): void {
  if (!drag || event.pointerId !== drag.pointerId) return;
  const body = bodies.get(drag.key);
  if (!body) return;
  const dx = event.clientX - drag.start.x;
  const dy = event.clientY - drag.start.y;
  if (!drag.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
  if (!drag.moved) {
    drag.moved = true;
    dragKey.value = drag.key;
    body.pinned = true;
    body.px = body.x;
    body.py = body.y;
  }
  body.x = Math.min(WIDTH - MARGIN_X, Math.max(MARGIN_X, drag.origin.x + dx * drag.scale));
  body.y = Math.min(HEIGHT - MARGIN_Y, Math.max(MARGIN_Y, drag.origin.y + dy * drag.scale));
  publish();
  wake();
}

function endDrag(event: PointerEvent): void {
  if (!drag || event.pointerId !== drag.pointerId) return;
  (event.currentTarget as Element).releasePointerCapture?.(event.pointerId);
  const body = bodies.get(drag.key);
  if (drag.moved) {
    // 拖动结束后浏览器仍会触发一次 click，需要吞掉，免得每次拖完都弹出详情
    suppressClick = drag.key;
    if (body) {
      body.pinned = false; // 松手后带着最后的速度，被弹簧拉回去
    }
  }
  drag = null;
  dragKey.value = "";
  wake();
}

function click(key: string, action: () => void): void {
  if (suppressClick === key) {
    suppressClick = "";
    return;
  }
  suppressClick = "";
  action();
}

function activate(item: Placed): void {
  if (item.node) emit("select", item.node);
  else emit("add", item.kind);
}

const KICK = 7;

function keydown(event: KeyboardEvent, key: string, action: () => void): void {
  const direction: Record<string, Point> = {
    ArrowLeft: { x: -KICK, y: 0 },
    ArrowRight: { x: KICK, y: 0 },
    ArrowUp: { x: 0, y: -KICK },
    ArrowDown: { x: 0, y: KICK },
  };
  const kick = direction[event.key];
  const body = bodies.get(key);
  if (kick && body) {
    // 键盘没有拖动手势，用一次推力代替：节点被推开后会弹回，并带动邻居
    event.preventDefault();
    body.vx += kick.x;
    body.vy += kick.y;
    wake();
  } else if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    action();
  }
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
  return `${KIND_NAMES[item.node.kind]} ${item.node.label}，${STATUS_NAMES[item.node.status]}，点击查看详情，方向键推动`;
}
</script>

<template>
  <div class="mesh-canvas">
    <svg
      ref="svg"
      class="mesh-topology"
      :viewBox="`0 0 ${WIDTH} ${HEIGHT}`"
      role="group"
      aria-label="计算服务网状拓扑，节点可拖动"
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
          :x1="hubPoint.x"
          :y1="hubPoint.y"
          :x2="pointOf(item.key, item.home).x"
          :y2="pointOf(item.key, item.home).y"
        />
      </g>

      <g
        v-for="item in placed"
        :key="item.key"
        class="mesh-vertex"
        :class="[
          `kind-${item.kind}`,
          item.node ? `status-${item.node.status}` : 'status-add',
          { selected: item.node && selectedKey === item.key, dragging: dragKey === item.key },
        ]"
        role="button"
        tabindex="0"
        :aria-label="ariaLabel(item)"
        :transform="`translate(${pointOf(item.key, item.home).x} ${pointOf(item.key, item.home).y})`"
        @pointerdown="startDrag($event, item.key)"
        @pointermove="moveDrag"
        @pointerup="endDrag"
        @pointercancel="endDrag"
        @click="click(item.key, () => activate(item))"
        @keydown="keydown($event, item.key, () => activate(item))"
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
        :class="{ selected: selectedKey === hubKey, dragging: dragKey === hubKey }"
        role="button"
        tabindex="0"
        :aria-label="`${KIND_NAMES.hub} ${hub.label}，点击查看详情，方向键推动`"
        :transform="`translate(${hubPoint.x} ${hubPoint.y})`"
        @pointerdown="startDrag($event, hubKey)"
        @pointermove="moveDrag"
        @pointerup="endDrag"
        @pointercancel="endDrag"
        @click="click(hubKey, () => emit('select', hub))"
        @keydown="keydown($event, hubKey, () => emit('select', hub))"
      >
        <circle class="mesh-halo" r="62" />
        <circle class="mesh-shape" r="46" />
        <text class="mesh-hub-title" y="-3" text-anchor="middle">{{ hub.label }}</text>
        <text v-if="hub.sub" class="mesh-hub-sub" y="16" text-anchor="middle">{{ hub.sub }}</text>
      </g>
    </svg>
  </div>
</template>
