<script setup lang="ts">
import { apiGet, apiPost, errorText, type Schemas } from '~/composables/useApi'
import { formatDateTime } from '~/composables/useFormat'
import { useInboxStore } from '~/stores/inbox'

type InboxPage = Schemas['InboxPage']
type DeliveryAttempt = Schemas['DeliveryAttempt']

const inbox = useInboxStore()

const data = ref<InboxPage | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)
const unreadOnly = ref(false)
const severity = ref('')
const status = ref<'open' | 'resolved' | ''>('')
const search = ref('')
const page = ref(1)
const size = ref(20)
const delivery = ref<Record<number, DeliveryAttempt[]>>({})

/** Ввод в поиске не должен дёргать сервер на каждую букву. */
const searchDebounced = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null
watch(search, (value) => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    searchDebounced.value = value
  }, 350)
})

const hasFilters = computed(
  () => Boolean(unreadOnly.value || severity.value || status.value || search.value),
)

async function load(showSpinner = false) {
  if (showSpinner) loading.value = true
  try {
    const params = new URLSearchParams({
      page: String(page.value),
      size: String(size.value),
    })
    if (unreadOnly.value) params.set('unread_only', 'true')
    if (severity.value) params.set('severity', severity.value)
    if (status.value) params.set('status', status.value)
    if (searchDebounced.value.trim()) params.set('search', searchDebounced.value.trim())
    data.value = await apiGet<InboxPage>(`/notifications?${params.toString()}`)
    error.value = null
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    loading.value = false
  }
}

function resetFilters() {
  unreadOnly.value = false
  severity.value = ''
  status.value = ''
  search.value = ''
  searchDebounced.value = ''
}

async function markRead(id: number) {
  await apiPost(`/notifications/${id}/read`)
  await Promise.all([load(), inbox.refresh()])
}

async function markAll() {
  await apiPost('/notifications/read-all')
  await Promise.all([load(), inbox.refresh()])
}

async function toggleDelivery(id: number) {
  if (delivery.value[id]) {
    const rest = { ...delivery.value }
    delete rest[id]
    delivery.value = rest
    return
  }
  delivery.value = {
    ...delivery.value,
    [id]: await apiGet<DeliveryAttempt[]>(`/notifications/${id}/delivery`),
  }
}

const statusLabels: Record<string, string> = {
  sent: 'доставлено',
  pending: 'в очереди',
  failed: 'ошибка',
  skipped: 'пропущено',
}

onMounted(() => void load(true))
onUnmounted(() => {
  if (searchTimer) clearTimeout(searchTimer)
})

watch([unreadOnly, severity, status, searchDebounced, size], () => {
  page.value = 1
  void load(true)
})
watch(page, () => void load(true))
</script>

<template>
  <section>
    <div class="row header">
      <h1>Инбокс</h1>
      <span class="muted">непрочитанных: {{ inbox.unread }}</span>

      <div class="row actions">
        <input
          v-model="search"
          class="search"
          type="search"
          placeholder="Поиск по правилу или объекту"
        />

        <select v-model="status">
          <option value="">Инциденты любые</option>
          <option value="open">Инцидент открыт</option>
          <option value="resolved">Инцидент закрыт</option>
        </select>

        <label class="row check">
          <input v-model="unreadOnly" type="checkbox" />
          только непрочитанные
        </label>

        <button v-if="hasFilters" type="button" @click="resetFilters">Сбросить</button>
        <button :disabled="!inbox.unread" @click="markAll">Отметить всё прочитанным</button>
      </div>
    </div>

    <SeverityCounters
      :counts="data?.severity_counts"
      :active="severity"
      @select="severity = $event"
    />

    <StateBlock
      :loading="loading"
      :error="error"
      :empty="!data?.items.length"
      :empty-text="hasFilters ? 'По заданным условиям ничего нет' : 'Уведомлений нет'"
    >
      <div class="grid list">
        <article
          v-for="item in data?.items ?? []"
          :key="item.notification_id"
          class="panel item"
          :class="{ unread: !item.read_at }"
        >
          <div class="row top">
            <SeverityBadge :severity="item.severity" />
            <NuxtLink :to="`/incidents/${item.incident_id}`" class="title">
              {{ item.rule_name }}
            </NuxtLink>
            <span class="muted">{{ item.object_name ?? '—' }}</span>
            <span v-if="item.topic_name" class="badge">{{ item.topic_name }}</span>
            <span class="muted time">{{ formatDateTime(item.created_at) }}</span>
          </div>

          <p v-if="item.summary" class="summary">{{ item.summary }}</p>

          <div class="row bottom">
            <span class="muted">
              инцидент {{ item.incident_status === 'open' ? 'открыт' : 'закрыт' }}
            </span>
            <span v-if="item.read_at" class="muted">
              прочитано {{ formatDateTime(item.read_at) }}
            </span>
            <div class="row buttons">
              <button type="button" @click="toggleDelivery(item.notification_id)">
                {{ delivery[item.notification_id] ? 'Скрыть доставку' : 'История доставки' }}
              </button>
              <button v-if="!item.read_at" type="button" @click="markRead(item.notification_id)">
                Прочитано
              </button>
            </div>
          </div>

          <table v-if="delivery[item.notification_id]" class="delivery">
            <thead>
              <tr>
                <th>Канал</th>
                <th>Статус</th>
                <th>Попыток</th>
                <th>Отправлено</th>
                <th>Ошибка</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="attempt in delivery[item.notification_id]" :key="attempt.notification_id">
                <td>{{ attempt.channel }}</td>
                <td>{{ statusLabels[attempt.status] ?? attempt.status }}</td>
                <td>{{ attempt.attempts }}</td>
                <td class="mono">{{ formatDateTime(attempt.sent_at) }}</td>
                <td class="muted">{{ attempt.last_error ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </article>
      </div>

      <div class="panel pager">
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
  </section>
</template>

<style scoped>
.header {
  margin-bottom: 16px;
}

.header h1 {
  margin: 0;
}

.actions {
  margin-left: auto;
  gap: 8px;
}

.search {
  width: 240px;
}

.check {
  gap: 6px;
  color: var(--text-dim);
}

/* Карточки — отдельные панели, поэтому пагинации нужна своя */
.pager {
  margin-top: 10px;
  padding: 4px 16px;
}

.list {
  gap: 8px;
}

/* Карточка — компактная: в инбоксе за смену их сотни, воздух тут только мешает */
.item {
  padding: 10px 14px;
}

.item.unread {
  border-left: 3px solid var(--gpn-orange);
}

.top {
  margin-bottom: 4px;
  gap: 10px;
}

.title {
  font-weight: 600;
}

.time {
  margin-left: auto;
  font-size: 12px;
}

.summary {
  margin: 0 0 6px;
  line-height: 1.5;
}

.bottom {
  gap: 10px;
  font-size: 12px;
}

.buttons {
  margin-left: auto;
  gap: 8px;
}

.buttons button {
  padding: 3px 10px;
  font-size: 12px;
}

.delivery {
  margin-top: 12px;
}
</style>
