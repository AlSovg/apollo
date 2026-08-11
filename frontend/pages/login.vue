<script setup lang="ts">
import { errorText } from '~/composables/useApi'
import { useAuthStore } from '~/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const config = useRuntimeConfig()

const username = ref('')
const password = ref('')
const error = ref<string | null>(null)
const busy = ref(false)

/**
 * Учётки быстрого входа для демонстрации. Задаются переменными сборки
 * (`NUXT_PUBLIC_DEMO_USER`, `NUXT_PUBLIC_DEMO_ADMIN`) в формате «логин:пароль»;
 * если их нет — кнопок нет, и в сборке не остаётся никаких паролей.
 */
function parseDemo(value: string): { username: string; password: string } | null {
  const separator = value.indexOf(':')
  if (separator < 1) return null
  return {
    username: value.slice(0, separator),
    password: value.slice(separator + 1),
  }
}

const demoUser = computed(() => parseDemo(config.public.demoUser))
const demoAdmin = computed(() => parseDemo(config.public.demoAdmin))
const hasDemo = computed(() => Boolean(demoUser.value || demoAdmin.value))

async function login(asUsername: string, asPassword: string) {
  error.value = null
  busy.value = true
  try {
    await auth.login(asUsername, asPassword)
    const next = typeof route.query.next === 'string' ? route.query.next : '/'
    await navigateTo(next)
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    busy.value = false
  }
}

function submit() {
  return login(username.value, password.value)
}

/** Поля заполняются видимо: на демонстрации важно, под кем именно вошли. */
function loginAs(account: { username: string; password: string }) {
  username.value = account.username
  password.value = account.password
  return login(account.username, account.password)
}
</script>

<template>
  <form class="panel login" @submit.prevent="submit">
    <h1>Вход в систему</h1>
    <p class="muted">Платформа интеллектуального управления оповещениями</p>

    <label>
      Логин
      <input v-model="username" autocomplete="username" required autofocus />
    </label>

    <label>
      Пароль
      <input v-model="password" type="password" autocomplete="current-password" required />
    </label>

    <p v-if="error" class="error">{{ error }}</p>

    <button class="primary" type="submit" :disabled="busy">
      {{ busy ? 'Проверяем…' : 'Войти' }}
    </button>

    <div v-if="hasDemo" class="demo">
      <span class="muted hint">Демонстрационный стенд</span>
      <div class="row demo-buttons">
        <button
          v-if="demoUser"
          type="button"
          :disabled="busy"
          @click="loginAs(demoUser)"
        >
          Войти как пользователь
        </button>
        <button
          v-if="demoAdmin"
          type="button"
          :disabled="busy"
          @click="loginAs(demoAdmin)"
        >
          Войти как администратор
        </button>
      </div>
    </div>
  </form>
</template>

<style scoped>
.login {
  width: 380px;
  display: flex;
  flex-direction: column;
  gap: 14px;
  box-shadow: var(--shadow-strong);
  /* Фирменная полоса сверху вместо логотипа: чужие изображения в контур
     не тащим, а узнаваемость даёт цвет */
  border-top: 4px solid var(--gpn-orange);
}

label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--text-dim);
}

.error {
  color: var(--error);
  margin: 0;
}

.demo {
  border-top: 1px solid var(--border);
  padding-top: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.hint {
  font-size: 12px;
}

.demo-buttons {
  gap: 8px;
}

.demo-buttons button {
  flex: 1;
  font-size: 13px;
}
</style>
