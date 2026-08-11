import { useAuthStore } from '~/stores/auth'

/**
 * Доступ к экранам.
 *
 * Настоящая проверка прав — на бэкенде; здесь только навигация, чтобы
 * пользователь не смотрел на пустые экраны с ошибками 403.
 */
export default defineNuxtRouteMiddleware(async (to) => {
  const auth = useAuthStore()

  if (!auth.checked) {
    await auth.fetchUser()
  }

  if (to.path === '/login') {
    return auth.isAuthenticated ? navigateTo('/') : undefined
  }

  if (!auth.isAuthenticated) {
    return navigateTo({ path: '/login', query: { next: to.fullPath } })
  }

  if (to.path.startsWith('/admin') && !auth.isAdmin) {
    return navigateTo('/')
  }
})
