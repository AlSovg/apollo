<script setup lang="ts">
import { apiGet, errorText, type Schemas } from '~/composables/useApi'
import { formatDateTime } from '~/composables/useFormat'

type AuditPage = Schemas['AuditPage']

/**
 * Журнал аудита. Действия администраторов журналируются наравне с
 * пользовательскими — это отдельный пункт критерия ИБ.
 */
const ENTITY_TYPES = ['user', 'topic', 'subscription', 'source', 'role']

const data = ref<AuditPage | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

const entityType = ref('')
const action = ref('')
const actorId = ref('')
const page = ref(1)
const size = ref(50)

async function load(showSpinner = false) {
  if (showSpinner) loading.value = true
  try {
    const params = new URLSearchParams({ page: String(page.value), size: String(size.value) })
    if (entityType.value) params.set('entity_type', entityType.value)
    if (action.value) params.set('action', action.value)
    if (actorId.value) params.set('actor_id', actorId.value)
    data.value = await apiGet<AuditPage>(`/admin/audit?${params.toString()}`)
    error.value = null
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    loading.value = false
  }
}

function preview(value: Record<string, unknown> | null): string {
  if (!value || !Object.keys(value).length) return '—'
  return JSON.stringify(value)
}

onMounted(() => void load(true))
watch([entityType, action, actorId, size], () => {
  page.value = 1
  void load()
})
watch(page, () => void load())
</script>

<template>
  <section>
    <div class="row header">
      <h1>Журнал аудита</h1>
      <div class="row filters">
        <input v-model="actorId" placeholder="id актора" class="short" />
        <input v-model="action" placeholder="действие, напр. user.login" />
        <select v-model="entityType">
          <option value="">все сущности</option>
          <option v-for="type in ENTITY_TYPES" :key="type" :value="type">{{ type }}</option>
        </select>
      </div>
    </div>

    <StateBlock
      :loading="loading"
      :error="error"
      :empty="!data?.items.length"
      empty-text="Записей не найдено"
    >
      <div class="panel">
        <table>
          <thead>
            <tr>
              <th>Время</th>
              <th>Актор</th>
              <th>Действие</th>
              <th>Сущность</th>
              <th>Было</th>
              <th>Стало</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="entry in data?.items ?? []" :key="entry.id">
              <td class="mono">{{ formatDateTime(entry.created_at) }}</td>
              <td>
                {{ entry.actor_username ?? 'система' }}
                <div v-if="entry.actor_id" class="muted mono">#{{ entry.actor_id }}</div>
              </td>
              <td class="mono">{{ entry.action }}</td>
              <td>
                {{ entry.entity_type }}
                <span v-if="entry.entity_id" class="muted mono"> #{{ entry.entity_id }}</span>
              </td>
              <td class="mono value">{{ preview(entry.old_value) }}</td>
              <td class="mono value">{{ preview(entry.new_value) }}</td>
            </tr>
          </tbody>
        </table>

        <PagerBar
          :page="page"
          :size="size"
          :total="data?.total ?? 0"
          :items-on-page="data?.items.length"
          :sizes="[50, 100, 200]"
          @update:page="page = $event"
          @update:size="size = $event"
        />
      </div>
    </StateBlock>
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

.short {
  width: 120px;
}

.value {
  max-width: 260px;
  word-break: break-all;
  color: var(--text-dim);
}

</style>
