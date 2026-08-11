# Zabbix в системе: как встроен и что править

Карта интеграции для тех, кто дорабатывает приём событий Zabbix: где проходит
путь события, какие файлы за что отвечают и что менять при изменении формата.

Настройка самого Zabbix (media type, action, параметры) — в
`deploy/integrations/README.md`; здесь про код платформы.

## Путь события

```
Zabbix 7  ──вебхук──►  POST /api/v1/ingest/zabbix
                              │  один INSERT, ответ 202, разбора нет
                              ▼
                        raw_events (append-only)
                              │  normalizer: SELECT ... FOR UPDATE SKIP LOCKED
                              ▼
                    реестр event_sources → ZabbixAdapter
                              │  канонический вид + severity_map
                              ▼
              monitored_objects → incidents (upsert по external_key)
```

Ключевое свойство: **приём ничего не знает про Zabbix**. Незнакомое поле не
теряется — оно попадает в `raw_events.payload` целиком, а после разбора всё,
что не легло в канонические колонки, уезжает в `incidents.attrs`.

## Файлы

| Файл | Роль | Когда трогать |
|---|---|---|
| `backend/app/adapters/sources/zabbix.py` | преобразование payload → канонический вид | изменился состав или формат полей |
| `backend/app/adapters/sources/base.py` | канонические типы, `SeverityMap`, `clean()` | почти никогда: общий контракт всех источников |
| `backend/app/adapters/sources/registry.py` | `ADAPTERS` — имя класса → фабрика | новый адаптер (для Zabbix уже зарегистрирован) |
| `backend/tests/test_adapters_zabbix.py` | тесты разбора | вместе с адаптером |
| `backend/tests/fixtures/zabbix_problem.json`, `zabbix_resolved.json` | реальные payload'ы стенда | появился новый вид события |
| `deploy/integrations/zabbix7-webhook.js` | скрипт media type на стороне Zabbix | меняется набор отправляемых полей |
| `deploy/integrations/README.md` | таблица параметров media type | вместе со скриптом |

Ядро (`services/normalizer.py`, `services/ingest.py`, модели, API) при работе
с Zabbix **не меняется** — это требование `AGENTS.md`: новый источник или новый
формат не должен растекаться условиями по общему коду.

## Запись в реестре

Источник описан строкой в `event_sources` (админка «Источники»):

| Поле | Значение | Смысл |
|---|---|---|
| `code` | `zabbix` | часть URL приёма: `/api/v1/ingest/zabbix` |
| `kind` | `adapter` | преобразование не выражается маппингом |
| `adapter_class` | `ZabbixAdapter` | ключ в `ADAPTERS` |
| `timezone` | `Europe/Moscow` | payload приходит без таймзоны |
| `token_hash`, `hmac_secret` | — | аутентификация вебхука |
| `ip_allowlist` | пусто | пустой список = проверка выключена |

Часовой пояс здесь не косметика: Zabbix отдаёт `timestamp` (или пару
`date` + `time`) **без указания зоны** — это локальное время его сервера.
Захардкоженный UTC сдвинул бы все инциденты на несколько часов.

## Что адаптер берёт из payload

| Поле Zabbix | Куда попадает | Обязательное |
|---|---|---|
| `event_id` | `external_key` — ключ склейки повторов | да |
| `event_nseverity` | критичность через `severity_map` | да |
| `severity` | `severity_raw` — как называется в источнике | нет |
| `status` / `event_value` | вид события: проблема или восстановление | нет |
| `host_technical_name` / `host` | идентификатор объекта | да (хоть одно) |
| `host_ip`, `hostgroup` | адрес и группа объекта | нет |
| `trigger_id`, `trigger_name`, `trigger_expression` | правило | `trigger_name` — да |
| `message`, `item_lastvalue` | текст и значение метрики | нет |
| `event_tags` / `tags` | теги (список объектов, список строк или строка) | нет |
| `timestamp` либо `date` + `time` | время события | да |

Отсутствие обязательного поля — `AdapterError`: событие остаётся в
`raw_events` с заполненным `process_error`, конвейер не падает.

Числовое `event_nseverity` предпочтительнее строкового `severity`:
переименование и локализация уровней в интерфейсе Zabbix на него не влияют.

Хосты из группы `Test webhook group` помечаются `is_test=true` — такие
инциденты не идут в дашборд и не порождают уведомлений.

## Как менять

**Добавилось поле, которое нужно в карточке.** Правится только
`zabbix.py`: разбор в `parse()` и список `_CONSUMED_FIELDS` (иначе поле
продублируется в `attrs`). Плюс поле в теле скрипта media type и строка в
таблице параметров README.

**Изменились названия уровней критичности.** Кода нет вовсе — таблица
`severity_map` (источник + исходное значение → шкала 0–5). Для Zabbix там
цифры `0..5`.

**Появился новый вид события.** Смотреть `_kind()`: сначала `status`
(`PROBLEM` / `RESOLVED` / `OK`), потом `event_value` (`1` — проблема,
`0` — восстановление).

**Подключается второй экземпляр Zabbix.** Новый код источника в реестре с тем
же `adapter_class` и своим токеном — код не трогается совсем.

## Проверка

Не включать боевую отправку сразу: снять настоящее событие и прогнать через
админку «Источники» → «Тест». Payload разбирается и показывается результат,
в базу ничего не пишется.

Тесты адаптера:

```bash
cd backend && pytest tests/test_adapters_zabbix.py -q
```

Ручная отправка события мимо Zabbix (токен — из админки):

```bash
curl -X POST http://localhost/api/v1/ingest/zabbix \
  -H "Content-Type: application/json" -H "X-Webhook-Token: <токен>" \
  -d '{"event_id":"1042","event_nseverity":"4","severity":"High",
       "status":"PROBLEM","host":"web-01","host_ip":"10.20.0.11",
       "hostgroup":"Linux servers","trigger_id":"23451",
       "trigger_name":"Высокая загрузка CPU","message":"CPU 94%",
       "timestamp":"2026.08.11 10:53:52"}'
```

Ответ `202` означает «событие записано», а не «разобрано». Если инцидент не
появился:

```bash
docker compose exec db psql -U apollo -d apollo \
  -c "select id, received_at, process_error from raw_events
      where source='zabbix' order by id desc limit 5;"
```

`process_error` заполнен — ошибка разбора (смотреть адаптер); пусто и
`processed_at` пуст — событие ждёт нормализатор (смотреть его логи).

## Грабли, на которые уже наступали

- **`{EVENT.ID}` против `{EVENT.RECOVERY.ID}`** в media type. В
  recovery-операции Zabbix подставляет в `{EVENT.ID}` номер исходной проблемы —
  именно поэтому закрытие склеивается с тем же инцидентом. С
  `{EVENT.RECOVERY.ID}` вместо закрытия создастся новый инцидент.
- **Время восстановления.** Для закрытия нужны `{EVENT.RECOVERY.DATE}` и
  `{EVENT.RECOVERY.TIME}`, иначе инцидент закроется временем проблемы и
  длительность уйдёт в минус.
- **`*UNKNOWN*` и нерезолвнутые макросы.** Zabbix подставляет `*UNKNOWN*`
  вместо отсутствующего значения, а неподдерживаемый макрос приходит как есть
  (`{HOSTGROUP.NAME}`). Обе заглушки гасит `clean()` в `base.py` — отдельной
  обработки в адаптере не нужно.
