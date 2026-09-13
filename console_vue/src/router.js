import { createRouter, createWebHashHistory } from 'vue-router'

// hash 模式: console.py 静态托管无需历史回退配置
export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'dashboard', component: () => import('./views/Dashboard.vue') },
    { path: '/interact', name: 'interact', component: () => import('./views/Interact.vue') },
    { path: '/content', name: 'content', component: () => import('./views/Content.vue') }
  ]
})
