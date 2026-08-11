import { defineStore } from 'pinia'
import { apiGet, apiPost, type Schemas } from '~/composables/useApi'

type CurrentUser = Schemas['CurrentUserResponse']

/**
 * Состояние сессии.
 *
 * Токены здесь не хранятся вообще: они живут в httpOnly-cookie, а фронт знает
 * только роли и отображаемое имя.
 */
export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as CurrentUser | null,
    loading: false,
    checked: false,
  }),

  getters: {
    isAuthenticated: (state) => state.user !== null,
    roles: (state) => state.user?.roles ?? [],
    isAdmin: (state) => (state.user?.roles ?? []).includes('admin'),
    canSubscribe: (state) => {
      const roles = state.user?.roles ?? []
      // Наблюдатель смотрит дашборд, но не подписывается (раздел 2 docs/tz.md)
      return roles.includes('admin') || roles.includes('engineer')
    },
    displayName: (state) => state.user?.display_name || state.user?.username || '',
  },

  actions: {
    async fetchUser(): Promise<CurrentUser | null> {
      this.loading = true
      try {
        this.user = await apiGet<CurrentUser>('/auth/me')
      } catch {
        this.user = null
      } finally {
        this.loading = false
        this.checked = true
      }
      return this.user
    },

    async login(username: string, password: string): Promise<void> {
      await apiPost('/auth/login', { username, password })
      await this.fetchUser()
    },

    async logout(): Promise<void> {
      try {
        await apiPost('/auth/logout')
      } finally {
        this.user = null
        this.checked = true
      }
    },
  },
})
