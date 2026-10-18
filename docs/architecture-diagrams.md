# Схемы системы Apollo

Набор схем для описания системы: от контекста и сервисов до инфраструктуры и
масштабирования. Схемы написаны как Mermaid — рендерятся на GitHub и в IDE.
Пояснения и обоснование решений — в `docs/adr/` и `docs/architecture-scaling.md`.

Обозначения. Сплошная линия и рамка — реализовано в репозитории. Пунктир и
оранжевая заливка — целевая схема или отключённая на стенде интеграция. Серая
заливка — подстановка (мок) на стенде.

## 1. Контекст системы

Что находится вокруг платформы: кто присылает события, кому они доставляются и
от каких внешних систем зависит Apollo.

```mermaid
flowchart LR
    subgraph src["Системы мониторинга"]
        zbx["Zabbix 7"]
        sw["SolarWinds"]
        prom["Prometheus<br/>Alertmanager"]
    end

    core["<b>Apollo</b><br/>приём · нормализация · дедупликация<br/>маршрутизация · доставка · сводки"]

    subgraph users["Получатели"]
        eng["Дежурный инженер"]
        own["Владелец сервиса"]
        adm["Администратор"]
    end

    subgraph ext["Внешние системы"]
        dir["Каталог LDAP / AD<br/>вход и роли"]
        smtp["SMTP<br/>стенд: mailpit"]
        tc["TrueConf<br/>на стенде отключён"]
        llm["Языковая модель<br/>стенд: шаблон"]
    end

    zbx -->|"webhook"| core
    sw -->|"webhook"| core
    prom -->|"webhook"| core

    core -->|"веб-инбокс"| eng
    core -->|"веб-инбокс"| own
    adm -->|"темы, источники, аудит"| core

    core <-->|"проверка входа, группы"| dir
    core -->|"email"| smtp
    core -.->|"чат-бот"| tc
    core <-->|"сводка инцидента"| llm

    classDef mock fill:#e8ecef,stroke:#5c6b77,color:#1b2329
    classDef off fill:#fff1dc,stroke:#c77700,stroke-dasharray:5 4,color:#3a2a00
    class smtp,dir,llm mock
    class tc off
```
![alt text](image.png)

## 2. Сервисы и конвейер

Шесть отдельно запускаемых сервисов вокруг PostgreSQL. Стрелки к таблицам —
запись, обратные — выборка очереди. Вместо брокера сообщений очередью служат
сами таблицы (ADR-0001). Текст уведомления — сводка `llm`: `delivery` ждёт её
до 15 с, затем отправляет с исходным сообщением инцидента.

```mermaid
flowchart TB
    src["Источники"] -->|"POST /api/v1/ingest/{source}"| nginx
    ui["Браузер:<br/>инбокс, админка"] --> nginx

    nginx["<b>frontend</b><br/>nginx: статика + балансировка"] --> backend["<b>backend</b> × N<br/>FastAPI: API и приём"]

    subgraph pg["PostgreSQL 16"]
        raw[("raw_events<br/>append-only")]
        inc[("incidents<br/>incident_events<br/>monitored_objects")]
        notif[("notifications")]
        sum[("llm_summaries")]
        cfg[("topics · subscriptions<br/>users · roles")]
    end

    backend -->|"один INSERT, ответ 202"| raw
    backend <-->|"API, инбокс"| cfg
    backend -.->|"чтение"| inc
    backend -.->|"инбокс: текст сводки"| sum

    raw -->|"SKIP LOCKED"| norm["<b>normalizer</b> × N<br/>адаптер → канон → отпечаток"]
    norm -->|"upsert по source + external_key"| inc

    inc -->|"SKIP LOCKED"| router["<b>router</b> × N<br/>темы → подписчики"]
    cfg --> router
    router -->|"pending"| notif

    notif -->|"SKIP LOCKED"| delivery["<b>delivery</b> × N"]
    delivery --> ch1["web-инбокс"]
    delivery --> ch2["email"]
    delivery -.-> ch3["TrueConf"]

    inc -->|"SKIP LOCKED"| llm["<b>llm</b> × N<br/>сводка инцидента"]
    llm --> sum
    sum ==>|"текст уведомления,<br/>ожидание до 15 с"| delivery

    classDef off fill:#fff1dc,stroke:#c77700,stroke-dasharray:5 4,color:#3a2a00
    class ch3 off
```

## 3. Путь события от вебхука до письма

Приём не парсит и не ждёт обработки: вебхук превращается в одну строку
`raw_events`, остальное делают воркеры асинхронно. Содержимое письма и
карточки инбокса — сводка языковой модели.

```mermaid
sequenceDiagram
    autonumber
    participant S as Источник
    participant N as nginx
    participant B as backend
    participant DB as PostgreSQL
    participant W as normalizer
    participant R as router
    participant L as llm
    participant D as delivery
    participant M as SMTP / инбокс

    S->>N: POST /api/v1/ingest/zabbix
    N->>B: запрос на одну из реплик
    B->>B: токен источника, HMAC, лимит частоты
    B->>DB: INSERT raw_events
    B-->>S: 202 Accepted

    loop опрос очереди
        W->>DB: SELECT … FOR UPDATE SKIP LOCKED
        W->>W: адаптер → канонический вид, критичность, отпечаток
        W->>DB: upsert monitored_objects, incidents, incident_events
    end

    R->>DB: SELECT инциденты … SKIP LOCKED
    R->>DB: темы по match_rule → эффективные подписчики
    R->>DB: INSERT notifications (pending)

    loop опрос очереди
        L->>DB: SELECT инциденты без сводки … SKIP LOCKED
        L->>L: языковая модель → человекочитаемая сводка
        L->>DB: INSERT llm_summaries
    end

    D->>DB: SELECT notifications … SKIP LOCKED
    D->>DB: сводка инцидента из llm_summaries
    alt сводка готова
        D->>D: текст = сводка
    else 15 с ожидания прошли, сводки нет
        D->>D: текст = исходное сообщение инцидента
    end
    D->>M: доставка по каналу
    alt успех
        D->>DB: status = sent
    else временная ошибка
        D->>DB: next_retry_at через 1, 5, 30 минут
    else адреса нет или исчерпаны попытки
        D->>DB: status = failed
    end
```

## 4. Жизненный цикл инцидента и уведомления

Повторное событие с тем же отпечатком `(source, external_key)` не создаёт
нового инцидента, а увеличивает счётчик. Внутри окна подавления оно не
порождает и нового уведомления.

```mermaid
stateDiagram-v2
    direction LR

    state "Инцидент" as inc {
        [*] --> open
        open --> open: повтор события,<br/>счётчик +1
        open --> resolved: событие resolved
        resolved --> [*]
    }

    state "Уведомление" as ntf {
        [*] --> pending
        pending --> sent: доставлено
        pending --> pending: временная ошибка,<br/>повтор по расписанию
        pending --> failed: нет адреса или<br/>попытки исчерпаны
        pending --> skipped: подписка заглушена
        sent --> [*]
        failed --> [*]
        skipped --> [*]
    }
```

## 5. Модель данных

Пять слоёв: события, субъекты, подписки, доставка, служебное. Показаны основные
связи; полная схема с комментариями — `db/schema.sql`. `raw_events` и
`incident_events` секционированы по месяцам.

```mermaid
erDiagram
    event_sources ||--o{ raw_events : "источник"
    event_sources ||--o{ monitored_objects : ""
    event_sources ||--o{ severity_map : "карта критичности"
    monitored_objects ||--o{ incidents : ""
    incidents ||--o{ incident_events : "лента"
    incidents ||--o{ llm_summaries : "сводки"
    incidents ||--o{ notifications : ""

    users ||--o{ user_contacts : "адреса доставки"
    users ||--o{ user_roles : ""
    roles ||--o{ user_roles : ""
    roles ||--o{ role_mappings : "группа каталога → роль"
    users ||--o{ sessions : ""
    users ||--o{ audit_log : "действия"

    topics ||--o{ topic_roles : "обязательные роли"
    roles ||--o{ topic_roles : ""
    users ||--o{ subscriptions : "добровольные"
    topics ||--o{ subscriptions : ""
    users ||--o{ notifications : "получатель"
    topics ||--o{ notifications : "по какой теме"

    incidents {
        bigint id PK
        text source FK
        text external_key "уникален вместе с source"
        text status "open или resolved"
        smallint severity "0..5"
        int dedup_count "склеено событий"
    }
    notifications {
        bigint id PK
        bigint incident_id FK
        bigint user_id FK
        text channel "web, email, trueconf"
        text status "pending, sent, failed, skipped"
        timestamptz next_retry_at
    }
    raw_events {
        bigint id PK
        text source FK
        jsonb payload
        timestamptz received_at "ключ секции"
    }
```

## 6. Инфраструктура стенда

Схема ниже — один хост (dev и локальная проверка). Публичный стенд разнесён на
два узла — приложение и БД на разных машинах, см. [`topology.md`](topology.md)
и ADR-0006. Базовый файл описывает продовый контур, dev-оверлей добавляет
подстановки внешних систем и публикацию портов на `127.0.0.1`.

```mermaid
flowchart LR
    client["Браузер / источники"] -->|":80"| fe

    subgraph host["Хост: Docker Compose, проект apollo"]
        direction TB

        subgraph net["сеть internal — наружу не публикуется"]
            fe["frontend<br/>nginx-unprivileged :8080"]
            be["backend × 2<br/>:8000"]
            nm["normalizer × 2"]
            rt["router"]
            dl["delivery"]
            lm["llm"]
            db[("db<br/>PostgreSQL 16")]
        end

        vol[/"том pgdata"/]
        db --- vol

        subgraph dev["dev-оверлей — только для разработки"]
            ldap["ldap<br/>OpenLDAP-мок"]
            mail["mailpit<br/>SMTP :1025"]
            llmm["llm-mock"]
            sim["simulator<br/>эмулятор Zabbix и SolarWinds"]
        end
    end

    fe --> be
    be --> db
    nm --> db
    rt --> db
    dl --> db
    lm --> db
    be -.-> ldap
    dl -->|"письма"| mail
    sim -->|"вебхуки"| be

    mail -.->|"127.0.0.1:8025"| ui1["веб-интерфейс почты"]
    db -.->|"127.0.0.1:55432"| ui2["тесты pytest"]

    classDef mock fill:#e8ecef,stroke:#5c6b77,color:#1b2329
    class ldap,mail,llmm,sim mock
```

Ограничения контейнеров (все): non-root, `read_only` + tmpfs, `cap_drop: [ALL]`,
`no-new-privileges`, лимиты CPU и памяти. Наружу в проде опубликован только
порт 80.

## 7. Масштабирование: что и как

Каждый компонент масштабируется своим способом. Воркеры очереди и `backend` —
горизонтально, добавлением реплик; PostgreSQL — вертикально.

```mermaid
flowchart LR
    subgraph now["Сейчас: реплики на одном хосте"]
        direction TB
        nginx1["nginx"] -->|"распределение по DNS"| b1["backend-1"]
        nginx1 --> b2["backend-2"]
        b1 & b2 --> pg1[("PostgreSQL")]
        n1["normalizer-1"] & n2["normalizer-2"] -->|"SKIP LOCKED"| pg1
        d1["delivery"] -->|"SKIP LOCKED"| pg1
    end

    subgraph next["Следующий шаг: Docker Swarm, несколько хостов"]
        direction TB
        mesh["routing mesh"] --> fe2["frontend × 2"]
        fe2 --> bk["backend × N<br/>на разных узлах"]
        bk --> pgp[("primary")]
        wk["воркеры × N<br/>на разных узлах"] --> pgp
        pgp ==>|"потоковая репликация"| pgr[("read-реплика")]
        bk -.->|"чтение: дашборд, история"| pgr
    end

    now ==>|"тот же docker-compose.yml,<br/>секции deploy.replicas / placement"| next

    classDef target fill:#fff1dc,stroke:#c77700,stroke-dasharray:5 4,color:#3a2a00
    class mesh,fe2,bk,pgp,wk,pgr target
```

| Компонент | Как масштабируется | Механизм | Что ограничивает | Статус |
|---|---|---|---|---|
| `backend` | горизонтально | реплики за nginx, stateless (JWT в cookie) | пул соединений с БД; лимит частоты приёма считается на реплику | 2 реплики, не измерено |
| `normalizer` | горизонтально | `FOR UPDATE SKIP LOCKED` | БД | измерено: 1 → 3 |
| `delivery` | горизонтально | `SKIP LOCKED` | БД, скорость внешнего канала | измерено: 1 → 2 |
| `router`, `llm` | горизонтально | `SKIP LOCKED` (в коде есть) | БД; у `llm` — скорость модели | 1 реплика, не измерено |
| `frontend` (nginx) | горизонтально | реплики; при Compose порт 80 занимает одна | публикация порта | 1 реплика |
| PostgreSQL | вертикально + партиции | секции по месяцам | ядро и память узла | 1 инстанс |
| Read-реплика БД | целевая схема | потоковая репликация (ADR-0004) | ручное переключение | не реализовано |

Ручное масштабирование без правок кода:

```bash
docker compose up -d --scale backend=3 --scale normalizer=3 --scale delivery=2
```

## 8. Балансировка и отказ реплики

nginx резолвит имя `backend` через встроенный DNS Docker на каждый запрос
(кэш 10 секунд). Новые и пересозданные реплики подхватываются без перезапуска
nginx, стартовый адрес ротируется, при сбое реплики — повтор на соседней
(ADR-0003).

```mermaid
sequenceDiagram
    autonumber
    participant C as Клиент
    participant N as nginx
    participant D as Docker DNS
    participant B1 as backend-1
    participant B2 as backend-2

    C->>N: GET /api/v1/incidents
    N->>D: backend?
    D-->>N: адреса B1 и B2
    N->>B1: запрос
    B1-->>N: 200
    N-->>C: 200, X-Upstream-Addr: B1

    C->>N: GET /api/v1/incidents
    N->>B2: запрос (резолвер ротирует адреса)
    B2-->>N: 200
    N-->>C: 200, X-Upstream-Addr: B2

    Note over B1: реплика упала
    C->>N: GET /api/v1/incidents
    N-xB1: соединение не установлено
    N->>B2: proxy_next_upstream
    B2-->>N: 200
    N-->>C: 200, X-Upstream-Addr: B2
```

Почему не статический блок `upstream {}`: открытый nginx резолвит имена в нём
один раз при старте, и после пересоздания контейнера отдавал бы 502 на
устаревший адрес. Цена выбранного варианта — нет активных health-check и весов.

## 9. Как воркеры делят очередь

Координатор не нужен: строку, которую взяла одна транзакция, `SKIP LOCKED`
пропускает для остальных. Дублей нет, при падении воркера транзакция
откатывается, а строки возвращаются в очередь.

```mermaid
sequenceDiagram
    autonumber
    participant A as normalizer-1
    participant B as normalizer-2
    participant Q as raw_events

    par воркер 1
        A->>Q: SELECT … LIMIT 100 FOR UPDATE SKIP LOCKED
        Q-->>A: строки 1–100
    and воркер 2
        B->>Q: SELECT … LIMIT 100 FOR UPDATE SKIP LOCKED
        Note over Q: строки 1–100 заблокированы, пропускаются
        Q-->>B: строки 101–200
    end

    A->>A: обработка пачки
    B->>B: обработка пачки
    A->>Q: COMMIT, строки отмечены обработанными
    Note over B: воркер 2 упал до COMMIT
    B--xQ: откат транзакции
    Note over Q: строки 101–200 снова доступны любому воркеру
```

## 10. Что показали замеры

Один хост, `db` ограничена одним ядром и 512 МБ (`docs/load-test-report.md`,
1000 событий, 30 одновременных запросов).

| Показатель | 1 normalizer | 3 normalizer, 2 delivery |
|---|---|---|
| Приём → нормализация, p50 | 6.87 с | **0.79 с** |
| Приём → нормализация, p95 | 10.31 с | **3.30 с** |
| Вебхук → инцидент, p95 | 9.26 с | **2.78 с** |
| Фактическая частота приёма | 58.6 событий/с | **75.3 событий/с** |
| Инцидент → инбокс, p95 | 5.47 с | 21.94 с |

Вывод: первым упирается нормализатор (несколько запросов на событие), после его
расшивки узким местом становится PostgreSQL на одном ядре — задержка доставки
выросла. Репликация `backend` за nginx ещё не измерена.

## 11. Границы доверия

```mermaid
flowchart LR
    subgraph untrusted["Недоверенная зона"]
        src["Источники<br/>токен на источник,<br/>опциональная HMAC"]
        br["Браузер<br/>JWT в httpOnly cookie"]
    end

    subgraph edge["Периметр"]
        nginx["nginx<br/>CSP, лимит тела 2 МБ,<br/>заголовки безопасности"]
    end

    subgraph inner["Внутренняя сеть"]
        be["backend<br/>лимит частоты,<br/>RBAC по ролям,<br/>Pydantic extra=forbid"]
        wk["воркеры"]
        db[("PostgreSQL<br/>порт не публикуется")]
    end

    subgraph ext["Внешние системы"]
        dir["каталог"]
        smtp["SMTP"]
    end

    src -->|"HTTP :80"| nginx
    br -->|"HTTP :80"| nginx
    nginx --> be
    be --> db
    wk --> db
    be -->|"вход, группы"| dir
    wk -->|"письма"| smtp
```

Слабые места периметра на сегодня: TLS на nginx не терминируется (шаг 13 плана
сборки), поэтому трафик до nginx идёт по HTTP. Персональные данные в логи не
попадают; контакты доставки хранятся только у локальных пользователей.


[def]: image.png