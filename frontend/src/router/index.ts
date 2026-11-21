import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import FieldIdentityView from '@/views/FieldIdentityView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 固定路径必须排在 /:moduleKey 之前，否则会被动态段抢先匹配。
    { path: '/field-identity', name: 'field-identity', component: FieldIdentityView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
