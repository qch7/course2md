import { createRouter, createWebHistory } from "vue-router";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", name: "new", component: () => import("./views/NewCourse.vue") },
    {
      path: "/course/:courseId",
      name: "course",
      component: () => import("./views/NewCourse.vue"),
      props: true,
    },
    { path: "/job/:jobId", name: "job", component: () => import("./views/JobView.vue"), props: true },
    { path: "/workers", name: "workers", component: () => import("./views/WorkersView.vue") },
    { path: "/plugins", name: "plugins", component: () => import("./views/PluginsView.vue") },
    { path: "/account", name: "account", component: () => import("./views/AccountView.vue") },
    { path: "/login", name: "login", component: () => import("./views/LoginView.vue"), meta: { bare: true } },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});
