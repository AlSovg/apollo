<script setup lang="ts">
import { apiGet, errorText, type Schemas } from '~/composables/useApi'
import { formatDateTime, formatLatency, formatShare } from '~/composables/useFormat'

type Processing = Schemas['ProcessingResponse']
type Load = Schemas['LoadResponse']
type Delivery = Schemas['DeliveryResponse']
type Top = Schemas['TopResponse']
type Latency = Schemas['LatencyResponse']
type Integrations = Schemas['IntegrationsResponse']

const period = ref<'1h' | '24h' | '7d' | '30d'>('24h')

const processing = ref<Processing | null>(null)
const load = ref<Load | null>(null)
const delivery = ref<Delivery | null>(null)
const top = ref<Top | null>(null)
const latency = ref<Latency | null>(null)
const integrations = ref<Integrations | null>(null)

const loading = ref(true)
const error = ref<string | null>(null)

/**
 * Столбик графика под курсором: подпись рисуем сами, штатный title всплывает
 * с задержкой и перекрывает соседние столбики.
 */
const hovered = ref<{
  bucket: string
  value: number
  height: number
  sources: { source: string; events: number }[]
} | null>(null)

async function refresh(showSpinner = false) {
  if (showSpinner) loading.value = true
  // Иначе под графиком остаётся подпись к столбику из прошлого набора данных
  hovered.value = null
  const query = `?period=${period.value}`
  try {
    ;[processing.value, load.value, delivery.value, top.value, latency.value, integrations.value] =
      await Promise.all([
        apiGet<Processing>(`/admin/dashboard/processing${query}`),
        apiGet<Load>(`/admin/dashboard/load${query}`),
        apiGet<Delivery>(`/admin/dashboard/delivery${query}`),
        apiGet<Top>(`/admin/dashboard/top${query}&limit=5`),
        apiGet<Latency>(`/admin/dashboard/latency${query}`),
        apiGet<Integrations>('/admin/dashboard/integrations'),
      ])
    error.value = null
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    loading.value = false
  }
}

const statusLabels: Record<string, string> = {
  ok: 'работает',
  unavailable: 'недоступна',
  not_configured: 'не настроена',
  disabled: 'не используется',
}

/** Простая гистограмма без внешних библиотек: CDN в контуре запрещены. */
const loadSeries = computed(() => {
  const points = load.value?.events_per_minute ?? []
  const byBucket = new Map<string, Map<string, number>>()
  for (const point of points) {
    const sources = byBucket.get(point.bucket) ?? new Map<string, number>()
    sources.set(point.source, (sources.get(point.source) ?? 0) + point.events)
    byBucket.set(point.bucket, sources)
  }
  const entries = [...byBucket.entries()].slice(-60)
  const max = Math.max(
    1,
    ...entries.map(([, sources]) => [...sources.values()].reduce((a, b) => a + b, 0)),
  )
  return entries.map(([bucket, sources]) => {
    const value = [...sources.values()].reduce((a, b) => a + b, 0)
    return {
      bucket,
      value,
      height: Math.round((value / max) * 100),
      // Разбивка по источникам: столбик без неё говорит «был всплеск»,
      // а с ней — какая интеграция его дала
      sources: [...sources.entries()]
        .sort((a, b) => b[1] - a[1])
        .map(([source, events]) => ({ source, events })),
    }
  })
})

const latencyRows = computed(() =>
  (latency.value?.segments ?? []).map((segment) => ({
    ...segment,
    // Сравниваем по p95: медиана прячет всплески, p99 слишком чувствителен
    // к единичным выбросам
    meetsTarget:
      segment.target_ms === null || segment.target_ms === undefined || segment.p95_ms === null
        ? null
        : segment.p95_ms <= segment.target_ms,
  })),
)

onMounted(() => void refresh(true))
watch(period, () => void refresh(true))
</script>

<template>
  <section>
    <div class="row header">
      <h1>Состояние платформы</h1>
      <select v-model="period" class="period">
        <option value="1h">за час</option>
        <option value="24h">за сутки</option>
        <option value="7d">за неделю</option>
        <option value="30d">за месяц</option>
      </select>
      <button @click="refresh(true)">Обновить</button>
    </div>

    <StateBlock :loading="loading" :error="error">
      <div class="grid tiles">
        <div class="panel tile">
          <span class="muted">Принято событий</span>
          <b>{{ processing?.events_received ?? 0 }}</b>
        </div>
        <div class="panel tile">
          <span class="muted">Создано инцидентов</span>
          <b>{{ processing?.incidents_created ?? 0 }}</b>
        </div>
        <div class="panel tile accent">
          <span class="muted">Коэффициент дедупликации</span>
          <b>{{ formatShare(processing?.dedup_ratio) }}</b>
          <span class="muted">
            шума меньше в {{ (processing?.noise_reduction ?? 1).toFixed(1) }} раза
          </span>
        </div>
        <div class="panel tile">
          <span class="muted">Доставлено уведомлений</span>
          <b>{{ delivery?.delivered_percent ?? 0 }}%</b>
          <span class="muted">отправлено {{ delivery?.sent ?? 0 }}</span>
        </div>
        <div class="panel tile">
          <span class="muted">Ошибок нормализации</span>
          <b :class="{ warn: (processing?.normalization_errors ?? 0) > 0 }">
            {{ processing?.normalization_errors ?? 0 }}
          </b>
        </div>
        <div class="panel tile">
          <span class="muted">Очереди</span>
          <b>{{ load?.normalization_backlog ?? 0 }} / {{ load?.delivery_backlog ?? 0 }}</b>
          <span class="muted">нормализация / доставка</span>
        </div>
      </div>

      <div class="grid two">
        <div class="panel">
          <h2>Нагрузка: событий в минуту</h2>
          <div v-if="loadSeries.length" class="chart-wrap">
            <div class="chart" @mouseleave="hovered = null">
              <div
                v-for="point in loadSeries"
                :key="point.bucket"
                class="bar"
                :class="{ hovered: hovered?.bucket === point.bucket }"
                :style="{ height: `${point.height}%` }"
                @mouseenter="hovered = point"
              />
            </div>

            <div class="readout" :class="{ empty: !hovered }">
              <template v-if="hovered">
                <b>{{ hovered.value }}</b>
                <span class="muted">событий · {{ formatDateTime(hovered.bucket) }}</span>
                <span
                  v-for="item in hovered.sources"
                  :key="item.source"
                  class="badge source"
                >
                  {{ item.source }} — {{ item.events }}
                </span>
              </template>
              <span v-else class="muted">наведите курсор на столбик</span>
            </div>
          </div>
          <p v-else class="state">Событий за период не было</p>
          <p class="muted">всего событий: {{ load?.events_total ?? 0 }}</p>
        </div>

        <div class="panel">
          <h2>Задержки конвейера</h2>
          <table>
            <thead>
              <tr>
                <th>Участок</th>
                <th>Замеров</th>
                <th>p50</th>
                <th>p95</th>
                <th>p99</th>
                <th>Норматив</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="segment in latencyRows" :key="segment.segment">
                <td>{{ segment.segment }}</td>
                <td>{{ segment.samples }}</td>
                <td class="mono">{{ formatLatency(segment.p50_ms) }}</td>
                <td class="mono" :class="{ over: segment.meetsTarget === false }">
                  {{ formatLatency(segment.p95_ms) }}
                </td>
                <td class="mono">{{ formatLatency(segment.p99_ms) }}</td>
                <td>
                  <span v-if="segment.meetsTarget === null" class="muted">—</span>
                  <span v-else :class="segment.meetsTarget ? 'badge ok' : 'badge over'">
                    ≤ {{ formatLatency(segment.target_ms) }}
                  </span>
                </td>
              </tr>
            </tbody>
          </table>
          <p class="muted hint">
            Норматив проверяется по p95. Показатели считаются за весь выбранный период —
            нагрузочные прогоны в нём тоже учтены, поэтому для оценки штатного режима
            берите короткий период.
          </p>
        </div>
      </div>

      <div class="grid two">
        <div class="panel">
          <h2>Доставка по каналам</h2>
          <table>
            <thead>
              <tr>
                <th>Канал</th>
                <th>Доставлено</th>
                <th>Ошибок</th>
                <th>В очереди</th>
                <th>Пропущено</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="channel in delivery?.by_channel ?? []" :key="channel.channel">
                <td>{{ channel.channel }}</td>
                <td>{{ channel.sent }}</td>
                <td :class="{ warn: channel.failed > 0 }">{{ channel.failed }}</td>
                <td>{{ channel.pending }}</td>
                <td class="muted">{{ channel.skipped }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="delivery?.failures.length" class="failures">
            <h2>Причины отказов</h2>
            <p v-for="failure in delivery.failures" :key="failure.reason" class="muted">
              {{ failure.reason }} — {{ failure.count }}
            </p>
          </div>
        </div>

        <div class="panel">
          <h2>Топ правил и объектов</h2>
          <table>
            <thead>
              <tr>
                <th>Правило</th>
                <th>Инцидентов</th>
                <th>Событий</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="rule in top?.rules ?? []" :key="rule.name">
                <td>{{ rule.name }}</td>
                <td>{{ rule.incidents }}</td>
                <td class="muted">{{ rule.events }}</td>
              </tr>
            </tbody>
          </table>
          <table class="objects">
            <thead>
              <tr>
                <th>Объект</th>
                <th>Инцидентов</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="object in top?.objects ?? []" :key="object.name">
                <td>{{ object.name }}</td>
                <td>{{ object.incidents }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <div class="panel">
        <h2>Состояние интеграций</h2>
        <div class="grid two">
          <table>
            <thead>
              <tr>
                <th>Источник</th>
                <th>Активен</th>
                <th>Последнее событие</th>
                <th>Молчит</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="source in integrations?.sources ?? []" :key="source.code">
                <td>{{ source.name }}</td>
                <td>{{ source.is_active ? 'да' : 'нет' }}</td>
                <td class="mono">{{ formatDateTime(source.last_event_at) }}</td>
                <td :class="{ warn: (source.silent_minutes ?? 0) > 30 }">
                  {{ source.silent_minutes !== null ? `${source.silent_minutes} мин` : '—' }}
                </td>
              </tr>
            </tbody>
          </table>

          <table>
            <thead>
              <tr>
                <th>Внешняя система</th>
                <th>Состояние</th>
                <th>Примечание</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in integrations?.integrations ?? []" :key="item.name">
                <td>{{ item.name }}</td>
                <td :class="item.status === 'unavailable' ? 'warn' : ''">
                  {{ statusLabels[item.status] ?? item.status }}
                </td>
                <td class="muted">{{ item.detail ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
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

.period {
  margin-left: auto;
}

.tiles {
  grid-template-columns: repeat(6, 1fr);
  margin-bottom: 16px;
}

.tile {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.tile b {
  font-size: 26px;
  color: var(--gpn-blue);
}

/* Коэффициент дедупликации — главная цифра защиты, выделен акцентным цветом */
.tile.accent {
  border-color: var(--gpn-orange);
  border-width: 2px;
}

.tile.accent b {
  color: var(--gpn-orange);
}

.two {
  grid-template-columns: 1fr 1fr;
  margin-bottom: 16px;
}

.chart {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 140px;
  padding: 8px;
  background: var(--bg-panel-alt);
  border: 1px solid var(--border);
  border-radius: 6px;
}

.bar {
  flex: 1;
  min-width: 3px;
  min-height: 2px;
  background: var(--gpn-light-blue);
  border-radius: 2px 2px 0 0;
}

.bar:hover,
.bar.hovered {
  background: var(--gpn-orange);
}

/* Строка под графиком: не всплывающая подсказка, а фиксированное место —
   при чтении показателей курсор не должен перекрывать сам график */
.readout {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 8px;
  min-height: 26px;
}

.readout b {
  font-size: 18px;
  color: var(--gpn-orange);
  font-variant-numeric: tabular-nums;
}

.badge.source {
  border-color: var(--border-strong);
}

.hint {
  font-size: 12px;
  margin-bottom: 0;
}

/* Превышение норматива — единственное, что здесь стоит подсвечивать */
.over {
  color: var(--error);
}

.badge.over {
  border-color: var(--error);
  color: var(--error);
}

.badge.ok {
  border-color: var(--ok);
  color: var(--ok);
}

.objects {
  margin-top: 16px;
}

.failures {
  margin-top: 12px;
}

.warn {
  color: var(--warn);
}
</style>
