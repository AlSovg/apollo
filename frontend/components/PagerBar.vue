<script setup lang="ts">
/**
 * Пагинация: сколько строк показано из скольких, переходы и размер страницы.
 *
 * Одна на все списки — иначе на каждом экране заводится своя, и они
 * расходятся в поведении.
 */
const props = withDefaults(
  defineProps<{
    page: number
    size: number
    total: number
    sizes?: number[]
    itemsOnPage?: number
  }>(),
  { sizes: () => [20, 50, 100], itemsOnPage: undefined },
)

const emit = defineEmits<{ 'update:page': [number]; 'update:size': [number] }>()

const lastPage = computed(() => Math.max(1, Math.ceil(props.total / props.size)))
const from = computed(() => (props.total === 0 ? 0 : (props.page - 1) * props.size + 1))
const to = computed(() => {
  const shown = props.itemsOnPage ?? props.size
  return Math.min(props.total, (props.page - 1) * props.size + shown)
})

/** Номера страниц с многоточиями: 1 … 4 5 6 … 12 */
const pages = computed<(number | '…')[]>(() => {
  const last = lastPage.value
  const current = props.page
  if (last <= 7) return Array.from({ length: last }, (_, index) => index + 1)

  const result: (number | '…')[] = [1]
  const start = Math.max(2, current - 1)
  const end = Math.min(last - 1, current + 1)
  if (start > 2) result.push('…')
  for (let page = start; page <= end; page += 1) result.push(page)
  if (end < last - 1) result.push('…')
  result.push(last)
  return result
})

function go(page: number) {
  const target = Math.min(Math.max(1, page), lastPage.value)
  if (target !== props.page) emit('update:page', target)
}

function changeSize(event: Event) {
  emit('update:size', Number((event.target as HTMLSelectElement).value))
  emit('update:page', 1)
}
</script>

<template>
  <div class="pager">
    <span class="muted range">
      <template v-if="total">показано {{ from }}–{{ to }} из {{ total }}</template>
      <template v-else>ничего не найдено</template>
    </span>

    <div class="controls">
      <button type="button" :disabled="page === 1" title="Первая" @click="go(1)">«</button>
      <button type="button" :disabled="page === 1" @click="go(page - 1)">Назад</button>

      <button
        v-for="(item, index) in pages"
        :key="`${item}-${index}`"
        type="button"
        class="page"
        :class="{ active: item === page, gap: item === '…' }"
        :disabled="item === '…'"
        @click="typeof item === 'number' && go(item)"
      >
        {{ item }}
      </button>

      <button type="button" :disabled="page >= lastPage" @click="go(page + 1)">Вперёд</button>
      <button type="button" :disabled="page >= lastPage" title="Последняя" @click="go(lastPage)">
        »
      </button>
    </div>

    <label class="size muted">
      строк на странице
      <select :value="size" @change="changeSize">
        <option v-for="option in sizes" :key="option" :value="option">{{ option }}</option>
      </select>
    </label>
  </div>
</template>

<style scoped>
.pager {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 12px 4px 0;
  flex-wrap: wrap;
}

.range {
  min-width: 180px;
}

.controls {
  display: flex;
  align-items: center;
  gap: 4px;
  margin: 0 auto;
}

.controls button {
  min-width: 34px;
  padding: 5px 10px;
}

.page.active {
  background: var(--gpn-light-blue);
  border-color: var(--gpn-light-blue);
  color: #fff;
  font-weight: 600;
}

.page.gap {
  border-color: transparent;
  background: none;
  opacity: 1;
  cursor: default;
  padding: 5px 2px;
  min-width: 16px;
}

.size {
  display: flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}

.size select {
  padding: 4px 8px;
}
</style>
