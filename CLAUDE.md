# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Главное

**Сначала прочитай `AGENTS.md`** — это основной контекстный файл проекта: домен, стек, границы MVP, правила обработки событий, требования ИБ и ПДн. Его требования соблюдаются буквально; этот файл его не дублирует, а дополняет.

Проект — платформа интеллектуального управления оповещениями (кейс IT CAMP 2026, Газпром нефть): зонтичный слой над Zabbix и SolarWinds с нормализацией, дедупликацией, подписками и доставкой уведомлений. Конвейер, бэкенд, фронтенд и стенд (`deploy/`) реализованы; сборка шла по шагам из `docs/build-prompts.md`.

## Файлы репозитория

- `AGENTS.md` — контекст проекта, читать первым
- `docs/README.md` — оглавление документации; `docs/agentic-development.md` — как устроены файлы для агентов
- `docs/tz.md` — техническое задание: FR/NFR, контракт API, канонический формат события, план работ
- `docs/build-prompts.md` — пошаговые промпты сборки (шаги 0–14); порядок шагов нарушать нельзя, каждый шаг имеет критерии приёмки
- `docs/adr/` — архитектурные решения; не пересматривать без новой записи ADR
- `docs/integration-runbook.md` — задание для агента, продолжающего подключение боевых источников
- `db/schema.sql` — модель данных PostgreSQL 16 с комментариями; источник истины для схемы БД

Папка `local/` (`ФАЙЛИК ДЛЯ САШИ.md`, `trueconf_checkup.md`, SSH-ключи) в `.gitignore` и лежит только локально; в репозиторий не добавлять.

## Команды

```bash
docker compose up -d          # postgres + backend + frontend(nginx)
curl localhost/health         # живость; /health/ready — готовность с проверкой БД

# backend/ (FastAPI, Python 3.12)
alembic upgrade head          # миграции; downgrade base и повторный upgrade должны проходить
pytest                        # тесты (pytest-asyncio)
pytest tests/test_x.py::test_name   # один тест
ruff check                    # линт
mypy                          # strict-режим

# frontend/ (Nuxt 3, ssr: false, TypeScript)
npm run generate              # сборка в статику, раздаётся nginx
```

## Архитектура — что нужно понимать целиком

Конвейер (подробности и диаграмма — в `AGENTS.md` §6, `docs/tz.md` §5):

```
POST /api/v1/ingest/{source} → raw_events (append-only, один INSERT, сразу 202)
  → normalizer worker (SKIP LOCKED): адаптер → канонический вид → monitored_objects
    → fingerprint → upsert incidents → insert incident_events
  → router worker: темы по match_rule → эффективные подписчики → notifications (pending)
  → delivery workers: web-инбокс, email, TrueConf; llm worker — сводка
```

Инварианты, которые нельзя нарушать (полный список — `AGENTS.md` §6):

- Приём вебхука = один INSERT в `raw_events` + ответ 202 Accepted. Никакого парсинга на приёме.
- `raw_events` и `incident_events` — append-only; UPDATE бывает только у `incidents`, и только через `INSERT ... ON CONFLICT DO UPDATE` по `(source, external_key)`.
- Очередь — таблица в PostgreSQL с `FOR UPDATE SKIP LOCKED`. Kafka/RabbitMQ/Redis не добавлять.
- Нормализация, дедупликация, критичность — детерминированный код. LLM только для саммаризации инцидента.
- Новый источник = новый адаптер (`adapters/`) или декларативный маппинг в реестре `event_sources`, а не `if` в ядре.
- ПДн: ФИО/email/телефоны не хранятся (исключение на MVP — `user_contacts`); в логи не попадают адреса доставки, тела уведомлений и payload событий.
- Pydantic-схемы — `extra="forbid"`; запросы к БД — только через ORM.

Ключевой принцип: **сквозной путь важнее фич** — событие должно проходить весь конвейер (генератор → вебхук → инцидент → уведомление → инбокс) до добавления нового функционала.

## Соглашения

- Язык общения, комментариев и документации — русский; код, имена таблиц и полей — английский.
- Зависимости сверх перечисленных в `AGENTS.md` не добавлять.
- Модели SQLAlchemy — только `Mapped[...]` + `mapped_column`, без legacy `Column()`.
- Противоречие задачи с `AGENTS.md` — остановиться и спросить, не решать самостоятельно.
