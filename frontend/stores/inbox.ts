import { defineStore } from 'pinia'
import { apiGet, type Schemas } from '~/composables/useApi'

type UnreadCount = Schemas['UnreadCount']

/**
 * Счётчик непрочитанных в шапке.
 *
 * Обновление — поллингом: WebSocket не вводим, это лишний компонент в контуре
 * (раздел 2 AGENTS.md).
 */
export const useInboxStore = defineStore('inbox', {
  state: () => ({
    unread: 0,
    timer: null as ReturnType<typeof setInterval> | null,
  }),

  actions: {
    async refresh(): Promise<void> {
      try {
        const result = await apiGet<UnreadCount>('/notifications/unread-count')
        this.unread = result.unread
      } catch {
        // Молча: счётчик в шапке не повод показывать ошибку на весь экран
      }
    },

    startPolling(intervalMs = 15000): void {
      if (this.timer) return
      void this.refresh()
      this.timer = setInterval(() => void this.refresh(), intervalMs)
    },

    stopPolling(): void {
      if (this.timer) {
        clearInterval(this.timer)
        this.timer = null
      }
    },
  },
})
