<script setup lang="ts">
import { apiGet, errorText, type Schemas } from '~/composables/useApi'
import { formatAge, formatDateTime } from '~/composables/useFormat'

type IncidentPage = Schemas['IncidentPage']

/** Дашборд активных инцидентов. Обновление — поллингом раз в 10 секунд. */
const REFRESH_MS = 10000

const data = ref<IncidentPage | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

const status = ref<'open' | 'resolved' | ''>('open')
const severity = ref('')
const source = ref('')
const search = ref('')
const page = ref(1)
const size = ref(20)

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
  () => Boolean(severity.value || source.value || search.value) || status.value !== 'open',
)

function query(): string {
  const params = new URLSearchParams({ page: String(page.value), size: String(size.value) })
  if (status.value) params.set('status', status.value)
  if (severity.value) params.set('severity', severity.value)
  if (source.value) params.set('source', source.value)
  if (searchDebounced.value.trim()) params.set('search', searchDebounced.value.trim())
  return params.toString()
}

async function load(showSpinner = false) {
  if (showSpinner) loading.value = true
  try {
    data.value = await apiGet<IncidentPage>(`/incidents?${query()}`)
    error.value = null
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    loading.value = false
  }
}

function resetFilters() {
  status.value = 'open'
  severity.value = ''
  source.value = ''
  search.value = ''
  searchDebounced.value = ''
}

let timer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  void load(true)
  timer = setInterval(() => void load(), REFRESH_MS)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  if (searchTimer) clearTimeout(searchTimer)
})

watch([status, severity, source, searchDebounced, size], () => {
  page.value = 1
  void load(true)
})
watch(page, () => void load(true))
</script>

<template>
  <section>
    <div class="row header">
      <h1>Инциденты</h1>
      <span class="muted refresh">обновляется автоматически</span>

      <div class="row filters">
        <input
          v-model="search"
          class="search"
          type="search"
          placeholder="Поиск по правилу или объекту"
        />

        <select v-model="status">
          <option value="open">Открытые</option>
          <option value="resolved">Закрытые</option>
          <option value="">Все</option>
        </select>

        <select v-model="source">
          <option value="">Все источники</option>
          <option value="zabbix">Zabbix</option>
          <option value="solarwinds">SolarWinds</option>
          <option value="prometheus">Prometheus</option>
          <option value="generic">Универсальный приём</option>
        </select>

        <button v-if="hasFilters" type="button" @click="resetFilters">Сбросить</button>
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
      :empty-text="hasFilters ? 'По заданным условиям ничего нет' : 'Инцидентов нет — это хорошая новость'"
    >
      <div class="panel">
        <table>
          <thead>
            <tr>
              <th class="col-severity">Критичность</th>
              <th>Правило</th>
              <th>Объект</th>
              <th class="col-source">Источник</th>
              <th class="col-time">Открыт</th>
              <th class="col-age">Возраст</th>
              <th class="col-dedup">Повторов</th>
              <th class="col-status">Статус</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="incident in data?.items ?? []" :key="incident.id">
              <td><SeverityBadge :severity="incident.severity" :raw="incident.severity_raw" /></td>
              <td>
                <NuxtLink :to="`/incidents/${incident.id}`" class="rule">
                  {{ incident.rule_name }}
                </NuxtLink>
              </td>
              <td>
                <div>{{ incident.object_name ?? '—' }}</div>
                <div v-if="incident.object_ip" class="muted mono">{{ incident.object_ip }}</div>
              </td>
              <td class="muted">{{ incident.source }}</td>
              <td class="mono nowrap">{{ formatDateTime(incident.opened_at) }}</td>
              <td class="nowrap">{{ formatAge(incident.age) }}</td>
              <td class="center">
                <span :class="incident.dedup_count > 1 ? 'badge dedup' : 'muted'">
                  {{ incident.dedup_count }}
                </span>
              </td>
              <td>
                <span :class="incident.status === 'open' ? 'open' : 'muted'">
                  {{ incident.status === 'open' ? 'открыт' : 'закрыт' }}
                </span>
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
  </section>
</template>

<style scoped>
.header {
  margin-bottom: 16px;
}

.header h1 {
  margin: 0;
}

.refresh {
  font-size: 12px;
}

.filters {
  margin-left: auto;
  gap: 8px;
}

.search {
  width: 260px;
}

.rule {
  font-weight: 500;
}

.dedup {
  border-color: var(--gpn-orange);
  background: #fff4ec;
  color: var(--gpn-orange);
  font-weight: 600;
}

.open {
  color: var(--error);
  font-weight: 500;
}

.nowrap {
  white-space: nowrap;
}

.center {
  text-align: center;
}

.col-severity {
  width: 160px;
}

.col-source,
.col-status {
  width: 110px;
}

.col-time {
  width: 170px;
}

.col-age,
.col-dedup {
  width: 100px;
}
</style>
