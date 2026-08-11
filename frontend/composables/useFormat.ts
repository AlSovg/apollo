/** Общее форматирование: severity, время, длительность. */

export const SEVERITY_LABELS: Record<number, string> = {
  0: 'Не классифицировано',
  1: 'Информация',
  2: 'Предупреждение',
  3: 'Средняя',
  4: 'Высокая',
  5: 'Авария',
}

export function severityLabel(severity: number): string {
  return SEVERITY_LABELS[severity] ?? String(severity)
}

export function severityColor(severity: number): string {
  return `var(--sev-${Math.min(Math.max(severity, 0), 5)})`
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return '—'
  return new Date(value).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function formatTime(value: string | null | undefined): string {
  if (!value) return '—'
  return new Date(value).toLocaleTimeString('ru-RU')
}

/** Длительность в человекочитаемом виде: 2 ч 14 мин. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 60) return `${Math.round(seconds)} с`

  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} мин`

  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  if (hours < 24) return rest ? `${hours} ч ${rest} мин` : `${hours} ч`

  const days = Math.floor(hours / 24)
  return `${days} сут ${hours % 24} ч`
}

/** Доля 0..1 в процентах: 0.9182 → «91.8 %». */
export function formatShare(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(1)} %`
}

/** Задержка: миллисекунды до секунды, дальше секунды — шестизначные мс не читаются. */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—'
  if (ms < 1000) return `${Math.round(ms)} мс`
  return `${(ms / 1000).toFixed(2)} с`
}

/** Возраст инцидента приходит строкой ISO 8601 duration от Pydantic. */
export function formatAge(age: string | null | undefined): string {
  if (!age) return '—'
  const match = /^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$/.exec(age)
  if (!match) return age

  const [, days, hours, minutes, seconds] = match
  const total =
    Number(days ?? 0) * 86400 +
    Number(hours ?? 0) * 3600 +
    Number(minutes ?? 0) * 60 +
    Number(seconds ?? 0)
  return formatDuration(total)
}
