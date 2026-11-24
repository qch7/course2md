<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 18 });

const paths: Record<string, string> = {
  plus: "M12 5v14M5 12h14",
  trash: "M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3",
  search: "m20 20-4.5-4.5M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0",
  book: "M12 5.5C9 3.5 5.5 3.5 3 4.5v14c3-1 6-1 9 1 3-2 6-2 9-1v-14c-2.5-1-6-1-9 1Zm0 0v14",
  server:
    "M5 3h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Zm0 10h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2ZM7 7h.01M7 17h.01M11 7h6M11 17h6",
  puzzle: "M9 3H4v6a3 3 0 1 1 0 6v5h5a3 3 0 1 1 6 0h5v-5a3 3 0 1 0 0-6V3h-5a3 3 0 1 1-6 0Z",
  user: "M20 21v-2a6 6 0 0 0-6-6h-4a6 6 0 0 0-6 6v2M16 5a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  sun: "M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  moon: "M20.9 13A9 9 0 0 1 11 3.1 9 9 0 1 0 20.9 13Z",
  system: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 0v18m0-18a9 9 0 0 1 0 18",
  arrow: "M5 12h14m-6-6 6 6-6 6",
  up: "M12 19V5m-6 6 6-6 6 6",
  menu: "M4 6h16M4 12h16M4 18h16",
  chevron: "m9 5 7 7-7 7",
  link: "M10 13a5 5 0 0 0 7 .1l3-3a5 5 0 0 0-7.1-7.1l-1.7 1.7M14 11a5 5 0 0 0-7-.1l-3 3a5 5 0 0 0 7.1 7.1l1.7-1.7",
  settings: "M4 7h16M4 17h16M8 4v6M16 14v6",
  file: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6M8 13h8M8 17h5",
  spark: "m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3Z",
  loader: "M21 12a9 9 0 1 1-9-9",
};
</script>

<template>
  <svg
    class="ui-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.6"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="paths[name] ?? paths.book" />
  </svg>
</template>
