<script setup lang="ts">
import { apiGet, apiPost, apiPut, errorText, type Schemas } from '~/composables/useApi'
import { formatDateTime } from '~/composables/useFormat'
import { useAuthStore } from '~/stores/auth'

type UserPage = Schemas['UserPage']
type UserResponse = Schemas['UserResponse']
type SyncResult = Schemas['SyncResult']

const ROLES = ['admin', 'engineer', 'viewer']

const auth = useAuthStore()

/**
 * Свою учётку отключить нельзя — сервер отвечает 409, поэтому кнопки быть
 * не должно вовсе: администратор не обязан узнавать про запрет из ошибки.
 * Сверяемся по subject_id: это идентификатор учётки, известный обеим сторонам.
 */
function isSelf(user: UserResponse): boolean {
  return Boolean(auth.user && user.subject_id === auth.user.subject_id)
}

const data = ref<UserPage | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const busy = ref(false)
const notice = ref<string | null>(null)

const query = ref('')
const provider = ref('')
const page = ref(1)
const size = ref(20)

const editing = ref<UserResponse | null>(null)
const selectedRoles = ref<string[]>([])

async function load(showSpinner = false) {
  if (showSpinner) loading.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), size: String(size.value) })
    if (query.value) params.set('query', query.value)
    if (provider.value) params.set('provider', provider.value)
    data.value = await apiGet<UserPage>(`/admin/users?${params.toString()}`)
    error.value = null
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    loading.value = false
  }
}

function startEdit(user: UserResponse) {
  editing.value = user
  // Синхронизация перезапишет роли из каталога, поэтому руками правим только локальные
  selectedRoles.value = user.roles.filter((role) => role.source === 'local').map((r) => r.code)
}

async function saveRoles() {
  if (!editing.value) return
  busy.value = true
  try {
    await apiPut(`/admin/users/${editing.value.id}/roles`, { roles: selectedRoles.value })
    editing.value = null
    await load()
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    busy.value = false
  }
}

async function deactivate(user: UserResponse) {
  busy.value = true
  try {
    const result = await apiPost<Schemas['DeactivateResult']>(
      `/admin/users/${user.id}/deactivate`,
    )
    notice.value = `Учётка «${user.username}» отключена, сессий отозвано: ${result.sessions_revoked}. Подписки сохранены.`
    await load()
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    busy.value = false
  }
}

async function syncDirectory() {
  busy.value = true
  try {
    const result = await apiPost<SyncResult>('/admin/ldap/sync')
    notice.value = `Синхронизация: проверено ${result.checked}, обновлено ${result.updated}, отключено ${result.deactivated}.`
    await load()
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    busy.value = false
  }
}

onMounted(() => void load(true))
watch([query, provider, size], () => {
  page.value = 1
  void load()
})
watch(page, () => void load())
</script>

<template>
  <section>
    <div class="row header">
      <h1>Пользователи</h1>
      <div class="row filters">
        <input v-model="query" placeholder="поиск по логину" />
        <select v-model="provider">
          <option value="">все источники учёток</option>
          <option value="ldap">из каталога</option>
          <option value="local">локальные</option>
        </select>
        <button :disabled="busy" @click="syncDirectory">Синхронизировать с каталогом</button>
      </div>
    </div>

    <p v-if="notice" class="panel notice">{{ notice }}</p>

    <StateBlock
      :loading="loading"
      :error="error"
      :empty="!data?.items.length"
      empty-text="Пользователей нет"
    >
      <div class="panel">
        <table>
          <thead>
            <tr>
              <th>Логин</th>
              <th>Учётка</th>
              <th>Роли</th>
              <th>Последний вход</th>
              <th>Роли обновлены</th>
              <th>Статус</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr v-for="user in data?.items ?? []" :key="user.id">
              <td>
                <b>{{ user.username }}</b>
                <span v-if="isSelf(user)" class="badge self">это вы</span>
                <div class="muted mono">{{ user.subject_id }}</div>
              </td>
              <td class="muted">{{ user.provider === 'ldap' ? 'каталог' : 'локальная' }}</td>
              <td>
                <span
                  v-for="role in user.roles"
                  :key="role.code"
                  class="badge role"
                  :class="role.source"
                >
                  {{ role.code }}<template v-if="role.source === 'ldap'"> ·&nbsp;из каталога</template>
                </span>
                <span v-if="!user.roles.length" class="muted">—</span>
              </td>
              <td class="mono">{{ formatDateTime(user.last_login_at) }}</td>
              <td class="mono muted">{{ formatDateTime(user.roles_synced_at) }}</td>
              <td>
                <span :class="user.is_active ? '' : 'muted'">
                  {{ user.is_active ? 'активна' : 'отключена' }}
                </span>
                <div v-if="user.locked_until" class="warn">
                  заблокирована до {{ formatDateTime(user.locked_until) }}
                </div>
              </td>
              <td class="actions">
                <button :disabled="busy" @click="startEdit(user)">Роли</button>
                <button
                  v-if="user.is_active && !isSelf(user)"
                  class="danger"
                  :disabled="busy"
                  @click="deactivate(user)"
                >
                  Отключить
                </button>
              </td>
            </tr>
          </tbody>
        </table>

        <PagerBar
          :page="page"
          :size="size"
          :total="data?.total ?? 0"
          :items-on-page="data?.items.length"
          @update:page="page = $event"
          @update:size="size = $event"
        />
      </div>
    </StateBlock>

    <div v-if="editing" class="panel editor">
      <h2>Локальные роли: {{ editing.username }}</h2>
      <p class="muted hint">
        Роли из каталога здесь не меняются — их перезапишет ближайшая синхронизация.
      </p>
      <div class="row checks">
        <label
          v-for="role in ROLES"
          :key="role"
          class="check"
          :title="
            role === 'admin' && isSelf(editing)
              ? 'Снять с себя роль администратора нельзя'
              : undefined
          "
        >
          <input
            v-model="selectedRoles"
            type="checkbox"
            :value="role"
            :disabled="role === 'admin' && isSelf(editing)"
          />
          {{ role }}
        </label>
      </div>
      <div class="row">
        <button class="primary" :disabled="busy" @click="saveRoles">Сохранить</button>
        <button :disabled="busy" @click="editing = null">Отмена</button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.header {
  margin-bottom: 16px;
}

.header h1 {
  margin: 0;
}

.filters {
  margin-left: auto;
}

.notice {
  border-color: var(--accent);
  margin-bottom: 16px;
}

.editor {
  margin-top: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 480px;
}

.hint {
  margin: 0;
  font-size: 12px;
}

.check {
  display: flex;
  align-items: center;
  gap: 6px;
}

.badge.role {
  margin-right: 4px;
}

.badge.self {
  margin-left: 6px;
  border-color: var(--gpn-orange);
  color: var(--gpn-orange);
  background: #fff4ec;
}

.badge.role.ldap {
  border-color: var(--accent);
  color: var(--accent);
}

.actions {
  width: 200px;
}

.warn {
  color: var(--warn);
  font-size: 12px;
}

</style>
