<script setup lang="ts">
import { useAuthStore } from '~/stores/auth'
import { useInboxStore } from '~/stores/inbox'

const auth = useAuthStore()
const inbox = useInboxStore()
const route = useRoute()

const showChrome = computed(() => route.path !== '/login' && auth.isAuthenticated)

watch(
  () => auth.isAuthenticated,
  (authenticated) => {
    if (authenticated) inbox.startPolling()
    else inbox.stopPolling()
  },
  { immediate: true },
)

onUnmounted(() => inbox.stopPolling())

async function logout() {
  await auth.logout()
  await navigateTo('/login')
}
</script>

<template>
  <div class="shell">
    <header v-if="showChrome" class="topbar">
      <NuxtLink to="/" class="brand">Управление оповещениями</NuxtLink>

      <nav class="nav">
        <NuxtLink to="/">Инциденты</NuxtLink>
        <NuxtLink to="/inbox">
          Инбокс
          <span v-if="inbox.unread" class="counter">{{ inbox.unread }}</span>
        </NuxtLink>
        <NuxtLink v-if="auth.canSubscribe" to="/subscriptions">Подписки</NuxtLink>
        <template v-if="auth.isAdmin">
          <span class="divider" />
          <NuxtLink to="/admin/dashboard">Платформа</NuxtLink>
          <NuxtLink to="/admin/topics">Темы</NuxtLink>
          <NuxtLink to="/admin/sources">Источники</NuxtLink>
          <NuxtLink to="/admin/users">Пользователи</NuxtLink>
          <NuxtLink to="/admin/audit">Аудит</NuxtLink>
        </template>
      </nav>

      <div class="user">
        <span class="muted">{{ auth.displayName }}</span>
        <span class="badge">{{ auth.roles.join(', ') || 'без ролей' }}</span>
        <button type="button" @click="logout">Выйти</button>
      </div>
    </header>

    <main :class="showChrome ? 'content' : 'content bare'">
      <slot />
    </main>
  </div>
</template>

<style scoped>
.shell {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 24px;
  padding: 12px 24px;
  /* Фирменный градиент Газпром нефти */
  background: linear-gradient(135deg, var(--gpn-blue) 0%, var(--gpn-light-blue) 100%);
  color: #fff;
  box-shadow: var(--shadow-strong);
}

.brand {
  font-weight: 700;
  font-size: 15px;
  color: #fff;
  white-space: nowrap;
}

.brand:hover {
  text-decoration: none;
}

.nav {
  display: flex;
  align-items: center;
  gap: 16px;
  flex: 1;
}

.nav a {
  color: rgba(255, 255, 255, 0.75);
  padding: 4px 0;
  border-bottom: 2px solid transparent;
  font-weight: 500;
}

.nav a:hover {
  text-decoration: none;
  color: #fff;
}

.nav a.router-link-exact-active {
  color: #fff;
  /* Оранжевый — акцентный цвет бренда */
  border-bottom-color: var(--gpn-orange);
}

.divider {
  width: 1px;
  height: 20px;
  background: rgba(255, 255, 255, 0.3);
}

.counter {
  display: inline-block;
  min-width: 20px;
  padding: 0 6px;
  margin-left: 4px;
  border-radius: 999px;
  background: var(--gpn-orange);
  color: #fff;
  font-size: 12px;
  font-weight: 700;
  text-align: center;
}

.user {
  display: flex;
  align-items: center;
  gap: 12px;
  white-space: nowrap;
  color: rgba(255, 255, 255, 0.85);
}

.user .muted {
  color: rgba(255, 255, 255, 0.75);
}

.user .badge {
  background: rgba(255, 255, 255, 0.15);
  border-color: rgba(255, 255, 255, 0.35);
  color: #fff;
}

.user button {
  background: rgba(255, 255, 255, 0.12);
  border-color: rgba(255, 255, 255, 0.4);
  color: #fff;
}

.user button:hover {
  background: rgba(255, 255, 255, 0.22);
  border-color: #fff;
}

.content {
  flex: 1;
  padding: 24px;
  max-width: 1600px;
  width: 100%;
  margin: 0 auto;
}

.content.bare {
  display: flex;
  align-items: center;
  justify-content: center;
}
</style>
