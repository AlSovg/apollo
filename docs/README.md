# Документация Apollo

Оглавление. Корневой [`README.md`](../README.md) — быстрый старт и обзор,
здесь — всё остальное.

## Агентная разработка

Файлы, на которых держится работа с ИИ-агентом. Как они связаны и как устроены —
в [`agentic-development.md`](agentic-development.md).

| Файл | Назначение |
|---|---|
| [`AGENTS.md`](../AGENTS.md) | контекст и жёсткие правила проекта, читается первым |
| [`CLAUDE.md`](../CLAUDE.md) | команды и соглашения для Claude Code |
| [`tz.md`](tz.md) | техническое задание, контракт API, канонический формат |
| [`../db/schema.sql`](../db/schema.sql) | модель данных — источник истины |
| [`build-prompts.md`](build-prompts.md) | пошаговые промпты сборки, шаги 0–14 |
| [`adr/`](adr/README.md) | архитектурные решения и отвергнутые альтернативы |
| [`integration-runbook.md`](integration-runbook.md) | задание для агента: подключение боевых источников |

## Архитектура

| Файл | Назначение |
|---|---|
| [`architecture-diagrams.md`](architecture-diagrams.md) | схемы: контекст, сервисы, путь события, модель данных, инфраструктура, масштабирование |
| [`topology.md`](topology.md) | топология из двух узлов: схема, маршруты, разделение сервисов, интерфейсы, отказы, развёртывание |
| [`architecture-scaling.md`](architecture-scaling.md) | масштабирование и балансировка, соответствие требованиям задания |
| [`adr/`](adr/README.md) | ADR 0001–0006 |

## Развёртывание и интеграции

| Файл | Назначение |
|---|---|
| [`reviewer-guide.md`](reviewer-guide.md) | инструкция проверяющего: URL, учётки, основной сценарий |
| [`deployment.md`](deployment.md) | развёртывание dev и prod, эксплуатация |
| [`zabbix-integration.md`](zabbix-integration.md) | как Zabbix встроен в код |
| [`../deploy/integrations/README.md`](../deploy/integrations/README.md) | настройка боевых Zabbix 7 и SolarWinds |
| [`llm-setup.md`](llm-setup.md) | языковая модель: контур, железо, замеры |
| [`load-test-report.md`](load-test-report.md) | отчёт о нагрузочном тестировании |

## Материалы защиты

| Файл | Назначение |
|---|---|
| [`economics.md`](economics.md) | экономическое обоснование и анализ рынка |
| [`scoring.md`](scoring.md) | матрица соответствия критериям оценки |
| [`presentation.md`](presentation.md) | структура выступления и вопросы к защите |

## Прочее

| Путь | Назначение |
|---|---|
| [`assets/`](assets/) | исходные файлы логотипа |
| [`../backend/tests/fixtures/README.md`](../backend/tests/fixtures/README.md) | происхождение тестовых payload |
