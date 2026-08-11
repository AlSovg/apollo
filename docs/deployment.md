# Развёртывание и эксплуатация

Два контура из одних и тех же файлов: базовый `deploy/docker-compose.yml` —
продовый по умолчанию, `deploy/docker-compose.dev.yml` — оверлей для
разработки. Оверлей применяется только явно; всё, что нужно лишь на деве
(моки, публикация порта БД, демо-кнопки входа), живёт в нём и в прод не
попадает по построению.

## Состав сервисов

| Сервис | Роль | Контур |
|---|---|---|
| `db` | PostgreSQL 16 | оба |
| `backend` | FastAPI: API, приём вебхуков, миграции при старте | оба |
| `normalizer` | разбор raw_events → инциденты | оба |
| `router` | темы → подписчики → notifications | оба |
| `delivery` | доставка: инбокс, email, TrueConf | оба |
| `llm` | сводки инцидентов | оба |
| `frontend` | nginx: статика + прокси `/api`, `/health*` | оба |
| `ldap` | OpenLDAP-подстановка каталога | только dev |
| `trueconf` | мок TrueConf с тем же контрактом | только dev |
| `llm-mock` | мок OpenAI-совместимого API модели | только dev |

Все контейнеры: non-root, `read_only` + tmpfs, `cap_drop: [ALL]`,
`no-new-privileges`, лимиты CPU/памяти. Наружу опубликован только порт 80
(nginx); порт БД в проде не публикуется.

## Dev

```bash
cp .env.example .env      # заполнить; для дева хватает значений по умолчанию
cd deploy
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
docker compose exec backend alembic upgrade head   # миграции — явный шаг
curl localhost/health           # живость
curl localhost/health/ready     # готовность: реально проверяет БД
```

Что даёт оверлей:

- **Порт БД** на `127.0.0.1:55432` — против него идут тесты (`pytest` из
  `backend/`).
- **Моки** LDAP / TrueConf / LLM. Переключение с мока на реальную систему —
  смена URL в `.env`, код не меняется. Сейчас LLM смотрит на реальную ВМ
  с Qwen3 (см. `docs/llm-setup.md`), моки LDAP и LLM можно останавливать.
- **Демо-кнопки входа** («Войти как пользователь / администратор») — учётки
  задаются build-аргами только в оверлее. Обычная сборка идёт без них: в
  статике не остаётся ни кнопок, ни паролей.

Локальные учётки (`admin`, `duty`) заводятся разовым скриптом администратора —
в коде и сидах их нет. Пользователи из каталога появляются синхронизацией
с LDAP (кнопка в админке «Пользователи»).

Пересборка после правок:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build backend frontend
```

Типы фронта после изменения API:

```bash
cd frontend && npm run types:api    # openapi-typescript → types/api.d.ts
```

## Prod

```bash
cp .env.example .env    # заполнить ВСЕ секреты — пустой секрет валит старт
cd deploy
docker compose up -d --build
```

Отличия от дева — только отсутствием оверлея: нет моков, порт БД закрыт,
кнопок демо-входа нет. Обязательные значения `.env` описаны в
`.env.example`; ключевые группы:

- `POSTGRES_*`, `DATABASE_URL` — база;
- `AUTH_*` — секрет JWT, время жизни сессий;
- `LDAP_*` — каталог (реальный вместо мока);
- `SMTP_*`, `TRUECONF_*` — каналы доставки;
- `LLM_*` — endpoint модели (см. `docs/llm-setup.md`).

Миграции при старте не выполняются — это отдельный явный шаг после
развёртывания и после каждого обновления образа:

```bash
docker compose exec backend alembic upgrade head
```

`downgrade base` + повторный `upgrade` проходят — это проверяется тестом.

### Что ещё не закрыто для прода

Зафиксировано в плане как шаг 13 (обвязка ИБ), выполняется отдельно:

- TLS на nginx (сейчас HTTP);
- TLS до ВМ с моделью — ключ ходит открытым текстом;
- скан зависимостей (trivy / pip-audit) и фиксация версий с хешами.

## Подключение источников мониторинга

Боевые Zabbix 7 и SolarWinds — `deploy/integrations/README.md` (настройка
систем) и `integration-runbook.md` (порядок работ и текущее состояние).
Новый источник без кода: админка «Источники» → декларативный маппинг, проверка
кнопкой «Тест» — payload разбирается без записи в базу.

## Эксплуатация

```bash
cd deploy
docker compose ps                        # состояние
docker compose logs -f backend normalizer
docker compose restart normalizer        # безопасно: очередь в БД, событий не теряет
```

Очереди и задержки видны в интерфейсе: «Платформа» (роль admin) — принято
событий, очереди нормализации и доставки, задержки конвейера против нормативов
(вебхук → инцидент ≤ 2 с, событие → инбокс ≤ 5 с по p95).

Первичная диагностика, если события не превращаются в инциденты:

```bash
docker compose exec db psql -U apollo -d apollo \
  -c "select id, source, received_at, process_error from raw_events order by id desc limit 10;"
```

`process_error` заполнен — ошибка разбора: смотреть адаптер или маппинг
источника. Пусто и `processed_at` пуст — событие ждёт нормализатор: смотреть
его логи.

Логи — JSON в stdout (собираются `docker compose logs`). По требованиям ПДн
в них нет адресов доставки, тел уведомлений и payload событий.

## Нагрузка

Генератор и проверка сквозного пути:

```bash
backend/.venv/Scripts/python tools/load_generator.py \
  --base-url http://127.0.0.1 --rate 40 --duration 60 --real \
  --issue-tokens --admin-user admin --admin-password '...'
backend/.venv/Scripts/python tools/check_pipeline.py --minutes 5
```

Флаг `--test` помечает события тестовыми: они не порождают уведомлений и не
попадают в дашборд. Результаты последнего прогона — `docs/load-test-report.md`;
узкое место — нормализатор, лечится репликами сервиса.
