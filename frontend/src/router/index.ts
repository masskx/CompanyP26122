import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      redirect: '/maintenance',
    },
    {
      path: '/maintenance',
      name: 'maintenance',
      component: () => import('@/features/maintenance/MaintenanceView.vue'),
    },
    {
      path: '/:pathMatch(.*)*',
      redirect: '/maintenance',
    },
  ],
})

export default router
