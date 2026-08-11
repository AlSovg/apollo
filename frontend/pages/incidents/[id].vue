<script setup lang="ts">
import { apiGet, errorText, type Schemas } from '~/composables/useApi'
import { formatDateTime, formatDuration } from '~/composables/useFormat'

type IncidentCard = Schemas['IncidentCard']

const route = useRoute()
const incident = ref<IncidentCard | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

async function load() {
  loading.value = true
  try {
    incident.value = await apiGet<IncidentCard>(`/incidents/${route.params.id}`)
    error.value = null
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    loading.value = false
  }
}

const kindLabels: Record<string, string> = {
  problem: 'проблема',
  resolved: 'восстановление',
  ack: 'квитирование',
  update: 'обновление',
}

onMounted(load)
</script>

<template>
  <section>
    <NuxtLink to="/" class="back">← К списку инцидентов</NuxtLink>

    <StateBlock :loading="loading" :error="error">
      <template v-if="incident">
        <div class="row header">
          <h1>{{ incident.rule_name }}</h1>
          <SeverityBadge :severity="incident.severity" :raw="incident.severity_raw" />
          <span class="badge" :class="incident.status">
            {{ incident.status === 'open' ? 'открыт' : 'закрыт' }}
          </span>
          <a v-if="incident.url" :href="incident.url" target="_blank" rel="noreferrer noopener">
            Открыть в системе-источнике
          </a>
        </div>

        <div class="grid columns">
          <div class="panel">
            <h2>Сводка модели</h2>
            <p v-if="incident.summary" class="summary">{{ incident.summary.content }}</p>
            <p v-else class="muted">
              Сводки пока нет — модель могла быть недоступна. На доставку это не влияет.
            </p>
            <p v-if="incident.summary" class="muted mono">
              {{ incident.summary.model }} · промпт {{ incident.summary.prompt_version }}
              <template v-if="incident.summary.masked"> · с маскированием</template>
            </p>
          </div>

          <div class="panel">
            <h2>Объект и правило</h2>
            <dl>
              <dt>Объект</dt>
              <dd>{{ incident.object_name ?? '—' }}</dd>
              <dt>Адрес</dt>
              <dd class="mono">{{ incident.object_ip ?? '—' }}</dd>
              <dt>Группа</dt>
              <dd>{{ incident.object_group ?? '—' }}</dd>
              <dt>Источник</dt>
              <dd>{{ incident.source }}</dd>
              <dt>Ключ склейки</dt>
              <dd class="mono">{{ incident.external_key }}</dd>
              <dt>Правило</dt>
              <dd class="mono">{{ incident.rule_expr ?? incident.rule_key ?? '—' }}</dd>
            </dl>
          </div>

          <div class="panel">
            <h2>Хронология</h2>
            <dl>
              <dt>Открыт</dt>
              <dd>{{ formatDateTime(incident.opened_at) }}</dd>
              <dt>Закрыт</dt>
              <dd>{{ formatDateTime(incident.resolved_at) }}</dd>
              <dt>Длительность</dt>
              <dd>{{ formatDuration(incident.duration_s) }}</dd>
              <dt>Повторов схлопнуто</dt>
              <dd>
                <span :class="incident.dedup_count > 1 ? 'badge dedup' : ''">
                  {{ incident.dedup_count }}
                </span>
              </dd>
              <dt>Теги</dt>
              <dd>
                <span v-for="tag in incident.tags" :key="tag" class="badge tag">{{ tag }}</span>
                <span v-if="!incident.tags.length" class="muted">—</span>
              </dd>
            </dl>
          </div>
        </div>

        <div class="panel feed">
          <h2>Лента событий</h2>
          <table>
            <thead>
              <tr>
                <th>Время в источнике</th>
                <th>Получено</th>
                <th>Тип</th>
                <th>Значение</th>
                <th>Сообщение</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="event in incident.events" :key="event.id">
                <td class="mono">{{ formatDateTime(event.event_ts) }}</td>
                <td class="mono muted">{{ formatDateTime(event.received_at) }}</td>
                <td>{{ kindLabels[event.kind] ?? event.kind }}</td>
                <td class="mono">{{ event.value ?? '—' }}</td>
                <td class="message">{{ event.message ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </StateBlock>
  </section>
</template>

<style scoped>
.back {
  display: inline-block;
  margin-bottom: 12px;
}

.header {
  margin-bottom: 16px;
}

.header h1 {
  margin: 0;
}

.columns {
  grid-template-columns: repeat(3, 1fr);
  margin-bottom: 16px;
}

.summary {
  margin: 0 0 8px;
  line-height: 1.55;
}

dl {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 6px 16px;
  margin: 0;
}

dt {
  color: var(--text-dim);
  font-size: 13px;
}

dd {
  margin: 0;
}

.badge.open {
  border-color: var(--error);
  color: var(--error);
}

.badge.dedup {
  border-color: var(--warn);
  color: var(--warn);
}

.badge.tag {
  margin-right: 4px;
}

.message {
  white-space: pre-wrap;
  max-width: 640px;
}
</style>
