<script setup lang="ts">
import { useAuthStore } from '~/stores/auth'

/**
 * Домашняя страница: короткий рассказ о платформе для тех, кто открыл её
 * впервые. Данные с бэкенда не запрашивает — только статика и навигация.
 */
const auth = useAuthStore()

useHead({ title: 'О платформе — управление оповещениями' })

/** Показатели прототипа — из ТЗ, раздел 1.2. */
const metrics = [
  { value: '100', unit: 'событий/с', caption: 'приём без потерь на пике' },
  { value: '≤ 2', unit: 'секунды', caption: 'от вебхука до инцидента в БД' },
  { value: '≤ 5', unit: 'секунд', caption: 'от инцидента до записи в инбоксе' },
  { value: '≥ 80', unit: '%', caption: 'дубликатов схлопывается на шторме' },
]

const pipeline = [
  {
    title: 'Приём',
    text: 'Вебхук источника — одна запись в журнал сырых событий и сразу ответ 200. Разбор идёт отдельно, поэтому перезапуск обработчика не теряет события.',
  },
  {
    title: 'Нормализация',
    text: 'Адаптер источника приводит событие к каноническому виду: объект, правило, критичность, время. Правила детерминированные — один и тот же вход всегда даёт один и тот же результат.',
  },
  {
    title: 'Дедупликация',
    text: 'По отпечатку события повторы схлопываются в один инцидент со счётчиком. Шторм из сотен сообщений превращается в одну строку дашборда.',
  },
  {
    title: 'Маршрутизация',
    text: 'Инцидент сопоставляется с темами, темы — с подписками. Уведомление получают только те, кому этот контур действительно нужен.',
  },
  {
    title: 'Доставка',
    text: 'Веб-инбокс, электронная почта и TrueConf. Каждая попытка доставки фиксируется, недоставленное повторяется.',
  },
  {
    title: 'Сводка',
    text: 'Локальная языковая модель формирует человекочитаемое описание инцидента. На решения о критичности и маршрутах она не влияет.',
  },
]
</script>

<template>
  <section class="home">
    <div class="panel hero">
      <img src="/logo.png" alt="Логотип платформы" class="hero-logo" width="120" height="120" />
      <div class="hero-text">
        <h1>Платформа интеллектуального управления оповещениями</h1>
        <p class="lead">
          Зонтичный слой над системами мониторинга: принимает события от Zabbix, SolarWinds
          и других источников, приводит их к единому виду, убирает дубликаты и доставляет
          уведомления тем, кто за этот контур отвечает.
        </p>
        <p class="muted">
          Платформа не заменяет мониторинг — пороги, триггеры и сбор метрик остаются
          в источниках. Она отвечает за то, что происходит с событием после срабатывания.
        </p>
        <div class="row actions">
          <NuxtLink to="/"><button type="button" class="primary">Открыть инциденты</button></NuxtLink>
          <NuxtLink to="/inbox"><button type="button">Мой инбокс</button></NuxtLink>
          <NuxtLink v-if="auth.canSubscribe" to="/subscriptions">
            <button type="button">Подписки</button>
          </NuxtLink>
        </div>
      </div>
    </div>

    <div class="metrics">
      <div v-for="metric in metrics" :key="metric.caption" class="panel metric">
        <div class="metric-value">
          {{ metric.value }} <span class="metric-unit">{{ metric.unit }}</span>
        </div>
        <div class="muted">{{ metric.caption }}</div>
      </div>
    </div>

    <div class="panel">
      <h2>Как проходит событие</h2>
      <ol class="pipeline">
        <li v-for="(step, index) in pipeline" :key="step.title">
          <span class="step-number">{{ index + 1 }}</span>
          <div>
            <div class="step-title">{{ step.title }}</div>
            <div class="muted">{{ step.text }}</div>
          </div>
        </li>
      </ol>
    </div>

    <div class="two-up">
      <div class="panel">
        <h2>Зачем это нужно</h2>
        <ul class="facts">
          <li>5400 событий в сутки от семи систем мониторинга обрабатываются полностью.</li>
          <li>65 % оповещений закрывались без реакции — дедупликация и подписки убирают этот шум.</li>
          <li>48 человеко-часов в сутки уходило на ручную фильтрацию у дежурной смены.</li>
          <li>14 минут незамеченного простоя сокращаются до секунд до попадания в инбокс.</li>
        </ul>
      </div>

      <div class="panel">
        <h2>Что важно знать</h2>
        <ul class="facts">
          <li>Критичность и дедупликация считаются кодом, а не моделью — результат воспроизводим.</li>
          <li>Новый источник подключается адаптером или маппингом, без правок ядра.</li>
          <li>Персональные данные не хранятся, адреса доставки и тела уведомлений в логи не попадают.</li>
          <li>Брокера сообщений и внешнего кеша в контуре нет — очередь живёт в той же СУБД.</li>
        </ul>
      </div>
    </div>
  </section>
</template>

<style scoped>
.home {
  display: grid;
  gap: 16px;
  max-width: 1100px;
}

.hero {
  display: flex;
  gap: 24px;
  align-items: flex-start;
  padding: 24px;
}

.hero-logo {
  flex: none;
  width: 120px;
  height: 120px;
  border-radius: 12px;
  box-shadow: var(--shadow-strong);
}

.hero-text h1 {
  margin-bottom: 12px;
}

.lead {
  margin: 0 0 8px;
  font-size: 15px;
}

.hero-text p {
  margin: 0 0 8px;
}

.actions {
  margin-top: 16px;
  gap: 8px;
}

.actions a:hover {
  text-decoration: none;
}

.metrics {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
}

.metric-value {
  font-size: 26px;
  font-weight: 700;
  color: var(--gpn-blue);
  font-variant-numeric: tabular-nums;
}

.metric-unit {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-dim);
}

.pipeline {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 16px;
}

.pipeline li {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}

.step-number {
  flex: none;
  width: 26px;
  height: 26px;
  border-radius: 999px;
  background: var(--gpn-light);
  border: 1px solid var(--border-strong);
  color: var(--gpn-blue);
  font-weight: 700;
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.step-title {
  font-weight: 600;
  color: var(--gpn-blue);
  margin-bottom: 2px;
}

.two-up {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.facts {
  margin: 0;
  padding-left: 18px;
}

.facts li {
  margin-bottom: 6px;
}

.facts li:last-child {
  margin-bottom: 0;
}
</style>
