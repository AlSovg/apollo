<script setup lang="ts">
/** Состояния загрузки, пустоты и ошибки — обязательны на каждом экране. */
defineProps<{
  loading?: boolean
  error?: string | null
  empty?: boolean
  emptyText?: string
}>()
</script>

<template>
  <div v-if="loading" class="state">Загрузка…</div>
  <div v-else-if="error" class="state error">{{ error }}</div>
  <div v-else-if="empty" class="state">{{ emptyText ?? 'Ничего не найдено' }}</div>
  <slot v-else />
</template>
