<script setup lang="ts">
import { severityColor, severityLabel } from '~/composables/useFormat'

/**
 * Счётчики по уровням критичности.
 *
 * Цифра — главное, поэтому она крупная, а подпись под ней. Клик отбирает
 * ровно этот уровень: плитка обещает число, и список обязан показать его же.
 * Раньше отбиралось «этот уровень и выше», и плитка «Средняя · 11» открывала
 * 122 строки — цифра не сходилась с содержимым.
 */
const props = defineProps<{
  counts: Record<string, number> | undefined
  active?: string
}>()

const emit = defineEmits<{ select: [string] }>()

const levels = [5, 4, 3, 2, 1, 0]

function value(level: number): number {
  return props.counts?.[String(level)] ?? 0
}

/**
 * Сумма плиток, а не общее число строк в списке: счётчики показывают картину
 * без учёта выбранного уровня, и «всего» обязано считаться так же — иначе
 * при фильтре по критичности плитки не сходятся с итогом.
 */
const total = computed(() => levels.reduce((sum, level) => sum + value(level), 0))
</script>

<template>
  <div class="counters">
    <button
      v-for="level in levels"
      :key="level"
      type="button"
      class="chip"
      :class="{ active: active === String(level), empty: value(level) === 0 }"
      :style="{ '--chip-color': severityColor(level) }"
      :title="`Показать только уровень «${severityLabel(level)}»`"
      @click="emit('select', active === String(level) ? '' : String(level))"
    >
      <b>{{ value(level) }}</b>
      <span>{{ severityLabel(level) }}</span>
    </button>

    <button
      type="button"
      class="chip total"
      :class="{ active: !active }"
      @click="emit('select', '')"
    >
      <b>{{ total }}</b>
      <span>всего</span>
    </button>
  </div>
</template>

<style scoped>
.counters {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.chip {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  min-width: 124px;
  padding: 8px 12px;
  background: #fff;
  border: 1px solid var(--border);
  /* Цветная полоса слева читается лучше, чем цветной текст цифры */
  border-left: 4px solid var(--chip-color, var(--border-strong));
  border-radius: 6px;
  box-shadow: var(--shadow);
  text-align: left;
}

.chip b {
  font-size: 22px;
  line-height: 1.1;
  color: var(--chip-color, var(--gpn-blue));
  font-variant-numeric: tabular-nums;
}

.chip span {
  font-size: 12px;
  color: var(--text-dim);
  white-space: nowrap;
}

.chip:hover:not(:disabled) {
  background: var(--gpn-light);
  border-color: var(--border-strong);
  border-left-color: var(--chip-color, var(--border-strong));
}

.chip.active {
  background: var(--gpn-light);
  border-color: var(--gpn-light-blue);
  border-left-color: var(--chip-color, var(--gpn-light-blue));
  box-shadow: 0 0 0 2px rgba(0, 102, 204, 0.15);
}

/* Пустые уровни не должны отвлекать от тех, где действительно есть работа */
.chip.empty b {
  color: var(--text-dim);
  opacity: 0.55;
}

.chip.total {
  border-left-color: var(--gpn-blue);
  margin-left: auto;
}

.chip.total b {
  color: var(--gpn-blue);
}
</style>
