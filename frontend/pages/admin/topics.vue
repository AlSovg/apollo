<script setup lang="ts">
import { apiDelete, apiGet, apiPatch, apiPost, apiPut, errorText, type Schemas } from '~/composables/useApi'
import { SEVERITY_LABELS } from '~/composables/useFormat'

type TopicResponse = Schemas['TopicResponse']
type MatchRule = Schemas['MatchRule']

const ROLES = ['admin', 'engineer', 'viewer']
const SOURCES = ['zabbix', 'solarwinds', 'prometheus', 'generic']

const topics = ref<TopicResponse[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const busy = ref(false)

const editing = ref<TopicResponse | null>(null)
const form = reactive({
  code: '',
  name: '',
  description: '',
  sources: [] as string[],
  severityMin: '' as string,
  objectGroups: '',
  tagsAny: '',
  tagsAll: '',
  roles: [] as string[],
})

function buildRule(): MatchRule {
  const rule: MatchRule = {}
  if (form.sources.length) rule.sources = form.sources
  if (form.severityMin !== '') rule.severity_min = Number(form.severityMin)
  const groups = form.objectGroups.split(',').map((item) => item.trim()).filter(Boolean)
  if (groups.length) rule.object_groups = groups
  const any = form.tagsAny.split(',').map((item) => item.trim()).filter(Boolean)
  if (any.length) rule.tags_any = any
  const all = form.tagsAll.split(',').map((item) => item.trim()).filter(Boolean)
  if (all.length) rule.tags_all = all
  return rule
}

function reset() {
  editing.value = null
  Object.assign(form, {
    code: '',
    name: '',
    description: '',
    sources: [],
    severityMin: '',
    objectGroups: '',
    tagsAny: '',
    tagsAll: '',
    roles: [],
  })
}

function edit(topic: TopicResponse) {
  editing.value = topic
  Object.assign(form, {
    code: topic.code,
    name: topic.name,
    description: topic.description ?? '',
    sources: topic.match_rule.sources ?? [],
    severityMin: topic.match_rule.severity_min?.toString() ?? '',
    objectGroups: (topic.match_rule.object_groups ?? []).join(', '),
    tagsAny: (topic.match_rule.tags_any ?? []).join(', '),
    tagsAll: (topic.match_rule.tags_all ?? []).join(', '),
    roles: topic.roles,
  })
}

async function load(showSpinner = false) {
  if (showSpinner) loading.value = true
  try {
    topics.value = await apiGet<TopicResponse[]>('/topics?include_inactive=true')
    error.value = null
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    loading.value = false
  }
}

async function save() {
  busy.value = true
  error.value = null
  try {
    const rule = buildRule()
    let topicId: number
    if (editing.value) {
      const updated = await apiPatch<TopicResponse>(`/topics/${editing.value.id}`, {
        name: form.name,
        description: form.description || null,
        match_rule: rule,
      })
      topicId = updated.id
    } else {
      const created = await apiPost<TopicResponse>('/topics', {
        code: form.code,
        name: form.name,
        description: form.description || null,
        match_rule: rule,
      })
      topicId = created.id
    }
    await apiPut(`/topics/${topicId}/roles`, { roles: form.roles })
    reset()
    await load()
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    busy.value = false
  }
}

async function deactivate(topic: TopicResponse) {
  busy.value = true
  try {
    await apiDelete(`/topics/${topic.id}`)
    await load()
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    busy.value = false
  }
}

function ruleSummary(rule: MatchRule): string {
  const parts: string[] = []
  if (rule.sources?.length) parts.push(`источники: ${rule.sources.join(', ')}`)
  if (rule.severity_min !== null && rule.severity_min !== undefined)
    parts.push(`критичность от ${rule.severity_min}`)
  if (rule.object_groups?.length) parts.push(`группы: ${rule.object_groups.join(', ')}`)
  if (rule.tags_any?.length) parts.push(`любой из тегов: ${rule.tags_any.join(', ')}`)
  if (rule.tags_all?.length) parts.push(`все теги: ${rule.tags_all.join(', ')}`)
  return parts.length ? parts.join('; ') : 'ловит все события'
}

onMounted(() => void load(true))
</script>

<template>
  <section>
    <h1>Каталог тем</h1>

    <div class="grid layout">
      <StateBlock :loading="loading" :error="error" :empty="!topics.length" empty-text="Тем пока нет">
        <div class="panel">
          <table>
            <thead>
              <tr>
                <th>Тема</th>
                <th>Правило отбора</th>
                <th>Обязательна для ролей</th>
                <th>Статус</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr v-for="topic in topics" :key="topic.id">
                <td>
                  <b>{{ topic.name }}</b>
                  <div class="muted mono">{{ topic.code }}</div>
                </td>
                <td class="muted">{{ ruleSummary(topic.match_rule) }}</td>
                <td>
                  <span v-for="role in topic.roles" :key="role" class="badge role">{{ role }}</span>
                  <span v-if="!topic.roles.length" class="muted">—</span>
                </td>
                <td>
                  <span :class="topic.is_active ? '' : 'muted'">
                    {{ topic.is_active ? 'активна' : 'отключена' }}
                  </span>
                </td>
                <td class="actions">
                  <button :disabled="busy" @click="edit(topic)">Изменить</button>
                  <button
                    v-if="topic.is_active"
                    class="danger"
                    :disabled="busy"
                    @click="deactivate(topic)"
                  >
                    Отключить
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </StateBlock>

      <form class="panel editor" @submit.prevent="save">
        <h2>{{ editing ? `Изменение темы «${editing.name}»` : 'Новая тема' }}</h2>

        <label>
          Код
          <input v-model="form.code" :disabled="!!editing" required pattern="[a-z0-9][a-z0-9\-_]*" />
        </label>

        <label>
          Название
          <input v-model="form.name" required />
        </label>

        <label>
          Описание
          <input v-model="form.description" />
        </label>

        <fieldset>
          <legend>Правило отбора</legend>
          <p class="muted hint">
            Условия соединяются логическим И, внутри списка — ИЛИ. Пустое правило
            ловит все события.
          </p>

          <div class="checks">
            <label v-for="source in SOURCES" :key="source" class="check">
              <input v-model="form.sources" type="checkbox" :value="source" />
              {{ source }}
            </label>
          </div>

          <label>
            Критичность не ниже
            <select v-model="form.severityMin">
              <option value="">любая</option>
              <option v-for="level in [0, 1, 2, 3, 4, 5]" :key="level" :value="String(level)">
                {{ level }} — {{ SEVERITY_LABELS[level] }}
              </option>
            </select>
          </label>

          <label>
            Группы объектов (через запятую)
            <input v-model="form.objectGroups" placeholder="prod-db, prod-web" />
          </label>

          <label>
            Любой из тегов
            <input v-model="form.tagsAny" placeholder="disk, cpu" />
          </label>

          <label>
            Все теги
            <input v-model="form.tagsAll" placeholder="prod" />
          </label>
        </fieldset>

        <fieldset>
          <legend>Обязательна для ролей</legend>
          <p class="muted hint">
            Носители роли получают подписку автоматически и не могут от неё отказаться.
          </p>
          <div class="checks">
            <label v-for="role in ROLES" :key="role" class="check">
              <input v-model="form.roles" type="checkbox" :value="role" />
              {{ role }}
            </label>
          </div>
        </fieldset>

        <div class="row">
          <button class="primary" type="submit" :disabled="busy">
            {{ editing ? 'Сохранить' : 'Создать' }}
          </button>
          <button v-if="editing" type="button" :disabled="busy" @click="reset">Отмена</button>
        </div>
      </form>
    </div>
  </section>
</template>

<style scoped>
.layout {
  grid-template-columns: 1fr 380px;
  align-items: start;
}

.editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--text-dim);
}

fieldset {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

legend {
  color: var(--text-dim);
  font-size: 12px;
  text-transform: uppercase;
}

.hint {
  margin: 0;
  font-size: 12px;
}

.checks {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.check {
  flex-direction: row;
  align-items: center;
  gap: 6px;
  color: var(--text);
}

.badge.role {
  margin-right: 4px;
}
</style>
