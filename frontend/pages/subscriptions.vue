<script setup lang="ts">
import { apiDelete, apiGet, apiPatch, apiPost, errorText, type Schemas } from '~/composables/useApi'
import { formatDateTime } from '~/composables/useFormat'

type SubscriptionsResponse = Schemas['SubscriptionsResponse']
type TopicResponse = Schemas['TopicResponse']

/**
 * Экран один, вкладки две. Фильтров по severity, тегам и объектам здесь нет
 * осознанно: вся сложность отбора живёт в правиле темы (раздел 4 AGENTS.md).
 */
const tab = ref<'mandatory' | 'available'>('mandatory')

const subscriptions = ref<SubscriptionsResponse | null>(null)
const topics = ref<TopicResponse[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const busy = ref(false)

const available = computed(() =>
  topics.value.filter((topic) => topic.state === 'available'),
)

async function load(showSpinner = false) {
  if (showSpinner) loading.value = true
  try {
    const [mine, catalog] = await Promise.all([
      apiGet<SubscriptionsResponse>('/subscriptions'),
      apiGet<TopicResponse[]>('/topics'),
    ])
    subscriptions.value = mine
    topics.value = catalog
    error.value = null
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    loading.value = false
  }
}

async function act(action: () => Promise<unknown>) {
  busy.value = true
  try {
    await action()
    await load()
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    busy.value = false
  }
}

const subscribe = (topicId: number) =>
  act(() => apiPost('/subscriptions', { topic_id: topicId, channels: ['web'] }))

const unsubscribe = (subscriptionId: number) =>
  act(() => apiDelete(`/subscriptions/${subscriptionId}`))

function mute(subscriptionId: number, hours: number) {
  const until = new Date(Date.now() + hours * 3600_000).toISOString()
  return act(() => apiPatch(`/subscriptions/${subscriptionId}`, { muted_until: until }))
}

const unmute = (subscriptionId: number) =>
  act(() => apiPatch(`/subscriptions/${subscriptionId}`, { muted_until: null }))

const originLabels: Record<string, string> = {
  mandatory_role: 'по роли',
  assigned: 'назначена администратором',
  self: 'добровольная',
}

onMounted(() => void load(true))
</script>

<template>
  <section>
    <h1>Подписки</h1>

    <div class="row tabs">
      <button :class="{ active: tab === 'mandatory' }" @click="tab = 'mandatory'">
        Обязательные ({{ subscriptions?.mandatory.length ?? 0 }})
      </button>
      <button :class="{ active: tab === 'available' }" @click="tab = 'available'">
        Доступные ({{ (subscriptions?.voluntary.length ?? 0) + available.length }})
      </button>
    </div>

    <StateBlock :loading="loading" :error="error">
      <div v-if="tab === 'mandatory'" class="panel">
        <p class="muted hint">
          Эти темы назначены ролью или администратором. Отписаться нельзя — можно
          только временно заглушить назначенную подписку.
        </p>
        <table v-if="subscriptions?.mandatory.length">
          <thead>
            <tr>
              <th>Тема</th>
              <th>Происхождение</th>
              <th>Каналы</th>
              <th>Глушение</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in subscriptions.mandatory" :key="item.topic_id">
              <td>
                <b>{{ item.topic_name }}</b>
                <div class="muted">{{ item.topic_description ?? '' }}</div>
              </td>
              <td>{{ originLabels[item.origin] ?? item.origin }}</td>
              <td class="mono">{{ item.channels.join(', ') }}</td>
              <td>
                <span v-if="item.muted_until" class="badge muted-badge">
                  до {{ formatDateTime(item.muted_until) }}
                </span>
                <span v-else class="muted">нет</span>
              </td>
              <td class="actions">
                <span v-if="item.origin === 'mandatory_role'" class="muted">
                  глушение не действует
                </span>
                <template v-else-if="item.subscription_id">
                  <button
                    v-if="!item.muted_until"
                    :disabled="busy"
                    @click="mute(item.subscription_id, 2)"
                  >
                    Заглушить на 2 ч
                  </button>
                  <button v-else :disabled="busy" @click="unmute(item.subscription_id)">
                    Снять глушение
                  </button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="state">Обязательных подписок нет</p>
      </div>

      <div v-else class="grid">
        <div class="panel">
          <h2>Мои добровольные подписки</h2>
          <table v-if="subscriptions?.voluntary.length">
            <thead>
              <tr>
                <th>Тема</th>
                <th>Каналы</th>
                <th>Глушение</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in subscriptions.voluntary" :key="item.topic_id">
                <td>
                  <b>{{ item.topic_name }}</b>
                  <div class="muted">{{ item.topic_description ?? '' }}</div>
                </td>
                <td class="mono">{{ item.channels.join(', ') }}</td>
                <td>
                  <span v-if="item.muted_until" class="badge muted-badge">
                    до {{ formatDateTime(item.muted_until) }}
                  </span>
                  <span v-else class="muted">нет</span>
                </td>
                <td class="actions">
                  <button
                    v-if="item.subscription_id && !item.muted_until"
                    :disabled="busy"
                    @click="mute(item.subscription_id, 2)"
                  >
                    Заглушить на 2 ч
                  </button>
                  <button
                    v-else-if="item.subscription_id"
                    :disabled="busy"
                    @click="unmute(item.subscription_id)"
                  >
                    Снять глушение
                  </button>
                  <button
                    v-if="item.subscription_id"
                    class="danger"
                    :disabled="busy"
                    @click="unsubscribe(item.subscription_id)"
                  >
                    Отписаться
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-else class="state">Вы ещё ни на что не подписались</p>
        </div>

        <div class="panel">
          <h2>Доступные темы</h2>
          <table v-if="available.length">
            <thead>
              <tr>
                <th>Тема</th>
                <th>Код</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr v-for="topic in available" :key="topic.id">
                <td>
                  <b>{{ topic.name }}</b>
                  <div class="muted">{{ topic.description ?? '' }}</div>
                </td>
                <td class="mono muted">{{ topic.code }}</td>
                <td class="actions">
                  <button class="primary" :disabled="busy" @click="subscribe(topic.id)">
                    Подписаться
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
          <p v-else class="state">Свободных тем нет</p>
        </div>
      </div>
    </StateBlock>
  </section>
</template>

<style scoped>
.tabs {
  margin-bottom: 16px;
  gap: 8px;
}

.tabs button.active {
  border-color: var(--accent);
  color: var(--accent);
}

.hint {
  margin: 0 0 12px;
}

.muted-badge {
  border-color: var(--warn);
  color: var(--warn);
}
</style>
