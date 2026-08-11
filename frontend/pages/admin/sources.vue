<script setup lang="ts">
import { apiDelete, apiGet, apiPost, errorText, type Schemas } from '~/composables/useApi'
import { formatDateTime } from '~/composables/useFormat'

type SourceResponse = Schemas['SourceResponse']
type SourceTestResult = Schemas['SourceTestResult']
type TokenIssued = Schemas['TokenIssued']

/**
 * Подключение новой системы мониторинга — операция администратора, а не
 * задача разработчику. Кнопка «тест» показывает, во что превратится payload,
 * поэтому маппинг настраивается за минуты, а не подбором вслепую.
 */
const sources = ref<SourceResponse[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const busy = ref(false)

const issued = ref<TokenIssued | null>(null)
const testResult = ref<SourceTestResult | null>(null)
const testFor = ref<string | null>(null)
const samplePayload = ref('')

const form = reactive({
  code: '',
  name: '',
  kind: 'mapping' as 'mapping' | 'canonical' | 'adapter',
  adapterClass: '',
  fieldMap: `{
  "external_key": "$.alerts[0].fingerprint",
  "severity_raw": "$.alerts[0].labels.severity",
  "object.external_id": "$.alerts[0].labels.instance",
  "object.name": "$.alerts[0].labels.instance",
  "rule.name": "$.alerts[0].labels.alertname",
  "message": "$.alerts[0].annotations.summary",
  "occurred_at": "$.alerts[0].startsAt",
  "kind": "$.status",
  "kind_map": {"firing": "problem", "resolved": "resolved"}
}`,
  timezone: '',
})

const kindLabels: Record<string, string> = {
  canonical: 'канонический JSON — кода не нужно',
  mapping: 'декларативный маппинг — кода не нужно',
  adapter: 'адаптер кодом',
}

async function load(showSpinner = false) {
  if (showSpinner) loading.value = true
  try {
    sources.value = await apiGet<SourceResponse[]>('/admin/sources')
    error.value = null
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    loading.value = false
  }
}

function parseFieldMap(): Record<string, unknown> {
  if (form.kind !== 'mapping') return {}
  return JSON.parse(form.fieldMap) as Record<string, unknown>
}

async function create() {
  busy.value = true
  error.value = null
  try {
    await apiPost('/admin/sources', {
      code: form.code,
      name: form.name,
      kind: form.kind,
      adapter_class: form.kind === 'adapter' ? form.adapterClass : null,
      field_map: parseFieldMap(),
      timezone: form.timezone || null,
    })
    await load()
  } catch (exception) {
    error.value =
      exception instanceof SyntaxError ? 'Маппинг не является корректным JSON' : errorText(exception)
  } finally {
    busy.value = false
  }
}

async function rotate(code: string, withHmac: boolean) {
  busy.value = true
  try {
    issued.value = await apiPost<TokenIssued>(
      `/admin/sources/${code}/rotate-token?with_hmac=${withHmac}`,
    )
    await load()
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    busy.value = false
  }
}

async function deactivate(code: string) {
  busy.value = true
  try {
    await apiDelete(`/admin/sources/${code}`)
    await load()
  } catch (exception) {
    error.value = errorText(exception)
  } finally {
    busy.value = false
  }
}

async function runTest(code: string) {
  busy.value = true
  testFor.value = code
  try {
    const body: Record<string, unknown> = { payload: JSON.parse(samplePayload.value) }
    if (form.kind === 'mapping' && form.fieldMap.trim()) {
      body.field_map = parseFieldMap()
    }
    testResult.value = await apiPost<SourceTestResult>(`/admin/sources/${code}/test`, body)
    error.value = null
  } catch (exception) {
    testResult.value = null
    error.value =
      exception instanceof SyntaxError ? 'Пример payload не является корректным JSON' : errorText(exception)
  } finally {
    busy.value = false
  }
}

onMounted(() => void load(true))
</script>

<template>
  <section>
    <h1>Реестр источников</h1>

    <div v-if="issued" class="panel token">
      <b>Токен для источника «{{ issued.code }}» выдан. Значение показывается один раз.</b>
      <p class="mono value">{{ issued.token }}</p>
      <p v-if="issued.hmac_secret" class="mono value">HMAC-секрет: {{ issued.hmac_secret }}</p>
      <button @click="issued = null">Скрыть</button>
    </div>

    <div class="grid layout">
      <StateBlock :loading="loading" :error="error">
        <div class="panel">
          <table>
            <thead>
              <tr>
                <th>Источник</th>
                <th>Способ подключения</th>
                <th>Токен</th>
                <th>Последнее событие</th>
                <th>Статус</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <tr v-for="source in sources" :key="source.code">
                <td>
                  <b>{{ source.name }}</b>
                  <div class="muted mono">{{ source.code }}</div>
                </td>
                <td class="muted">
                  {{ kindLabels[source.kind] ?? source.kind }}
                  <div v-if="source.adapter_class" class="mono">{{ source.adapter_class }}</div>
                </td>
                <td>
                  <span :class="source.token_set ? 'badge ok' : 'badge warn'">
                    {{ source.token_set ? 'выдан' : 'не выдан' }}
                  </span>
                  <span v-if="source.hmac_set" class="badge ok">HMAC</span>
                </td>
                <td class="mono">{{ formatDateTime(source.last_event_at) }}</td>
                <td>
                  <span :class="source.is_active ? '' : 'muted'">
                    {{ source.is_active ? 'активен' : 'отключён' }}
                  </span>
                </td>
                <td class="actions">
                  <button :disabled="busy" @click="rotate(source.code, false)">Токен</button>
                  <button :disabled="busy" @click="rotate(source.code, true)">Токен + HMAC</button>
                  <button :disabled="busy" @click="runTest(source.code)">Тест</button>
                  <button
                    v-if="source.is_active"
                    class="danger"
                    :disabled="busy"
                    @click="deactivate(source.code)"
                  >
                    Отключить
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="panel sample">
          <h2>Пример payload для кнопки «тест»</h2>
          <p class="muted">
            Вставьте тело реального вебхука. Разбор выполняется без записи в базу.
          </p>
          <textarea v-model="samplePayload" rows="8" spellcheck="false" />

          <div v-if="testResult" class="result">
            <p v-if="testResult.ok" class="ok">
              Разбор источника «{{ testFor }}» прошёл успешно:
            </p>
            <p v-else class="warn">Разбор не удался: {{ testResult.error }}</p>
            <ul v-if="testResult.problems.length">
              <li v-for="problem in testResult.problems" :key="problem">{{ problem }}</li>
            </ul>
            <pre v-if="testResult.event" class="mono">{{
              JSON.stringify(testResult.event, null, 2)
            }}</pre>
          </div>
        </div>
      </StateBlock>

      <form class="panel editor" @submit.prevent="create">
        <h2>Новый источник</h2>
        <p class="muted hint">
          Уровни подключения: канонический JSON и декларативный маппинг не требуют
          ни строчки кода, адаптер нужен только когда преобразование маппингом
          не выражается.
        </p>

        <label>
          Код
          <input v-model="form.code" required pattern="[a-z0-9][a-z0-9\-_]*" />
        </label>

        <label>
          Название
          <input v-model="form.name" required />
        </label>

        <label>
          Способ подключения
          <select v-model="form.kind">
            <option value="canonical">канонический JSON</option>
            <option value="mapping">декларативный маппинг</option>
            <option value="adapter">адаптер кодом</option>
          </select>
        </label>

        <label v-if="form.kind === 'adapter'">
          Класс адаптера
          <input v-model="form.adapterClass" placeholder="ZabbixAdapter" />
        </label>

        <label v-if="form.kind === 'mapping'">
          Маппинг полей
          <textarea v-model="form.fieldMap" rows="12" spellcheck="false" class="mono" />
        </label>

        <label>
          Таймзона источника
          <input v-model="form.timezone" placeholder="Europe/Moscow или +03:00" />
        </label>

        <button class="primary" type="submit" :disabled="busy">Зарегистрировать</button>
      </form>
    </div>
  </section>
</template>

<style scoped>
.layout {
  grid-template-columns: 1fr 420px;
  align-items: start;
}

.token {
  border-color: var(--accent);
  margin-bottom: 16px;
}

.token .value {
  word-break: break-all;
  background: var(--bg-panel-alt);
  padding: 8px;
  border-radius: 6px;
}

.editor,
.sample {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.sample {
  margin-top: 16px;
}

label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--text-dim);
}

textarea {
  width: 100%;
  resize: vertical;
}

.hint {
  margin: 0;
  font-size: 12px;
}

.badge.ok {
  border-color: var(--ok);
  color: var(--ok);
}

.badge.warn {
  border-color: var(--warn);
  color: var(--warn);
}

.result pre {
  background: var(--bg-panel-alt);
  padding: 10px;
  border-radius: 6px;
  overflow: auto;
  max-height: 320px;
}

.ok {
  color: var(--ok);
}

.warn {
  color: var(--warn);
}
</style>
