import "./styles/main.css";

import { VueQueryPlugin } from "@tanstack/vue-query";
import { createApp } from "vue";

import App from "./App.vue";
import { router } from "./router";
import "./stores/theme";
import { queryClient } from "./stores/query";

createApp(App).use(router).use(VueQueryPlugin, { queryClient }).mount("#app");
