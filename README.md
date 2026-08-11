# Apollo — платформа интеллектуального управления оповещениями

Зонтичный слой над системами мониторинга (Zabbix, SolarWinds, Prometheus):
принимает потоки событий, нормализует их к каноническому виду, дедуплицирует,
маршрутизирует по подпискам и доставляет уведомления — веб-инбокс, email,
TrueConf. Локальная языковая модель пишет к инцидентам человекочитаемые
сводки. Кейс IT CAMP 2026, заказчик — Газпром нефть.

## Что умеет

- **Приём** — `POST /api/v1/ingest/{source}`: один INSERT и ответ 202, никакого
  разбора на приёме. Токен на источник, опциональная HMAC-подпись, ограничение
  частоты на источник, лимит тела.
- **Нормализация** — адаптеры Zabbix 7 / SolarWinds / Prometheus Alertmanager
  плюс декларативный маппинг для новых источников: подключение без строчки кода
  в ядре.
- **Дедупликация** — события склеиваются в инциденты по отпечатку
  `(source, external_key)`; на нагрузочном прогоне поток сжимается в 12–20 раз.
- **Маршрутизация** — темы с правилами отбора (источник, критичность, группы
  объектов), подписки прямые и по ролям, глушение на время.
- **Доставка** — веб-инбокс, email, TrueConf; ретраи с расписанием, история
  доставки по каждому уведомлению.
- **Сводки** — Qwen3-14B на выделенной ВМ (vLLM, OpenAI-совместимый API);
  недоступность модели не влияет на доставку.
- **Администрирование** — реестр источников с кнопкой «Тест» (разбор payload
  без записи), темы, пользователи (LDAP + локальные), журнал аудита, дашборд
  платформы с задержками конвейера против нормативов.

## Архитектура

```
POST /ingest/{source} ──► raw_events (append-only)
        │  normalizer (FOR UPDATE SKIP LOCKED)
        ▼
   incidents ◄─ дедупликация ─ monitored_objects
        │  router: темы → эффективные подписчики
        ▼
  notifications (pending) ──► delivery workers ──► инбокс / email / TrueConf
        │
        └─ llm worker ──► сводка инцидента
```

Очередь — таблицы PostgreSQL с `FOR UPDATE SKIP LOCKED`; брокеров сообщений
в контуре нет намеренно. Вся логика нормализации и критичности —
детерминированный код, LLM используется только для сводок.

| Слой | Стек |
|---|---|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic v2 |
| БД | PostgreSQL 16, партиционирование событий по месяцам |
| Frontend | Nuxt 3 (`ssr: false`), Pinia, типы из OpenAPI |
| Развёртывание | Docker Compose, nginx-unprivileged, строгая CSP |
| LLM | vLLM, Qwen3-14B-AWQ, OpenAI-совместимый endpoint |

## Быстрый старт

```bash
cp .env.example .env        # заполнить секреты
cd deploy
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
docker compose exec backend alembic upgrade head              # схема и справочники
docker compose exec backend python -m app.cli create-admin breakglass
docker compose exec backend python -m app.seed                # темы и роли групп
curl localhost/health       # 200 — живой
```

Интерфейс — http://localhost. Логин — `breakglass` и пароль, который
`create-admin` печатает один раз в консоль (сохранить сразу).

`.env.example` по умолчанию ставит `AUTH_PROVIDER=local` — этого достаточно
для первого запуска и локальной разработки, боевой AD не нужен. Значение
`local` нужно будет заменить на `ldap` при переходе на боевой каталог —
без пересборки образов, только переменные окружения и пересоздание
`backend`/`delivery`, см. [«Вход через боевой AD»](#вход-через-боевой-ad)
ниже.

Подробности, продовый контур и отличия dev-оверлея —
[docs/deployment.md](docs/deployment.md).

## Документация

| Файл | Что внутри |
|---|---|
| `AGENTS.md` | контекст проекта: домен, границы MVP, требования ИБ и ПДн |
| `tz.md` | техническое задание: FR/NFR, контракт API, канонический формат |
| `docs/deployment.md` | развёртывание dev и prod, эксплуатация |
| `deploy/integrations/README.md` | подключение боевых Zabbix 7 и SolarWinds |
| `docs/zabbix-integration.md` | как Zabbix встроен в код и что править |
| `integration-runbook.md` | раннбук текущих работ по боевым источникам |
| `docs/llm-setup.md` | языковая модель: контур, ограничения железа, замеры |
| `docs/load-test-report.md` | отчёт о нагрузочном тестировании |
| `db/schema.sql` | модель данных с комментариями — источник истины по БД |

## Проверки качества

```bash
cd backend
ruff check && mypy && pytest    # линт, строгая типизация, ~340 тестов
```

Тесты идут против настоящего PostgreSQL (dev-оверлей публикует порт 55432)
и моков внешних систем — LDAP, TrueConf, LLM повторяют реальные контракты.

## Вход через боевой AD

Переключение со стандартного `AUTH_PROVIDER=local` (см. «Быстрый старт») на
боевой AD — отдельный шаг, не обязательный для первого запуска.

Проверено и работает: авторизация против реального Samba AD DC (`apollo-ad`,
домен `apollo.test`, контейнер поднят отдельно на ВМ TrueConf,
`89.208.86.149`), не против мока `osixia/openldap` из dev-оверлея.

Переключение — только переменные окружения, код адаптера не менялся:

```
AUTH_PROVIDER=ldap
LDAP_HOST=dc1.apollo.test
LDAP_PORT=636
LDAP_USE_SSL=true
LDAP_BASE_DN=DC=apollo,DC=test
LDAP_BIND_DN=ad.admin@apollo.test
LDAP_BIND_PASSWORD=<см. .env, в репозиторий не коммитится>
```

`LDAP_USERNAME_ATTRIBUTE=sAMAccountName`, `LDAP_ID_ATTRIBUTE=objectGUID`,
`LDAP_GROUP_OBJECT_CLASS=group` — значения по умолчанию из `config.py`,
менять не нужно (это атрибуты AD; для мок-OpenLDAP были бы `uid` /
`entryUUID` / `groupOfNames`).

Резолв имени из TLS-сертификата и CA домена — через `extra_hosts` и volume
в `deploy/docker-compose.dev.yml` (сервисы `backend`, `delivery`):
имя `dc1.apollo.test` резолвится на IP ВМ TrueConf, `deploy/ad/ca.pem`
монтируется в `/certs/ldap-ca.pem`. LDAPS обязателен: адаптер поднимает TLS с
`CERT_REQUIRED`, простой LDAP без шифрования Samba сама отклоняет
(`Strong(er) authentication required`).

Сервисная учётка для bind — `ad.admin` (группа `platform-admins`), не
встроенный `Administrator`: провайдеру нужен только read-доступ для поиска
записи пользователя, полные права домена ему не нужны.

После смены `.env` пересоздать `backend` и `delivery`:

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml \
  up -d --force-recreate backend delivery
```

Проверено логином `ad.duty`, `ad.admin` (роли синхронизируются из групп AD,
неверный пароль честно даёт 401).
