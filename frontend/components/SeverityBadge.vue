<script setup lang="ts">
import { severityColor, severityLabel } from '~/composables/useFormat'

const props = defineProps<{ severity: number; raw?: string | null }>()

const label = computed(() => severityLabel(props.severity))
const color = computed(() => severityColor(props.severity))
</script>

<template>
  <span class="severity" :style="{ borderColor: color, color }">
    {{ label }}<template v-if="raw"> · {{ raw }}</template>
  </span>
</template>

<style scoped>
.severity {
  display: inline-block;
  padding: 2px 8px;
  border: 1px solid;
  border-radius: 4px;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  background: #fff;
}
</style>
