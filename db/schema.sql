-- =============================================================================
-- Платформа интеллектуального управления оповещениями
-- Модель данных, PostgreSQL 16+
-- Версия 2.1 · 2026-08-10
--
-- Развитие черновика: к четырём слоям событий добавлены реестр источников,
-- пользователи, роли, каталог тем, подписки, доставка и аудит.
--
-- Слои:
--   A. События      event_sources → raw_events → monitored_objects
--                   → incidents → incident_events
--   B. Субъекты     users, roles, user_roles, role_mappings, user_contacts
--   C. Подписки     topics, topic_roles, subscriptions + вычисляемая эффективность
--   D. Доставка     notifications (она же очередь), llm_summaries
--   E. Служебное    severity_map, audit_log
--
-- Принцип: raw_events никогда не редактируется (кроме служебных processed_at /
-- process_error). Нормализованные слои — производные, их можно пересчитать.
-- =============================================================================


-- =============================================================================
-- A0. РЕЕСТР ИСТОЧНИКОВ
--
-- Подключение новой системы мониторинга = строка здесь. Ядро не меняется.
-- =============================================================================

CREATE TABLE event_sources (
    code        text PRIMARY KEY,          -- 'zabbix', 'solarwinds', 'prometheus'
    name        text NOT NULL,
    kind        text NOT NULL,             -- adapter | mapping | canonical

    -- Для kind='adapter' — имя класса SourceAdapter.
    adapter_class text,

    -- Для kind='mapping' — декларативное соответствие полей.
    -- {
    --   "external_key":      "$.alert.objectId",
    --   "severity_raw":      "$.alert.severity",
    --   "object.external_id":"$.node.objectId",
    --   "object.name":       "$.node.name",
    --   "message":           "$.alert.message",
    --   "occurred_at":       "$.timestamp",
    --   "kind_map":          {"trigger": "problem", "reset": "resolved"}
    -- }
    field_map   jsonb NOT NULL DEFAULT '{}',

    -- Аутентификация вебхуков этого источника
    token_hash   bytea,                    -- sha256 статического токена
    hmac_secret  bytea,                    -- секрет подписи тела
    ip_allowlist inet[] NOT NULL DEFAULT '{}',

    timezone    text,                      -- для источников без таймзоны в payload (Zabbix)
    is_active   boolean NOT NULL DEFAULT true,

    last_event_at timestamptz,             -- когда последний раз что-то приходило
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT event_sources_kind_chk CHECK (kind IN ('adapter', 'mapping', 'canonical')),
    -- Адаптерный источник обязан указать класс, маппинговый — непустой маппинг
    CONSTRAINT event_sources_impl_chk CHECK (
        (kind = 'adapter'   AND adapter_class IS NOT NULL) OR
        (kind = 'mapping'   AND field_map <> '{}') OR
        (kind = 'canonical')
    )
);

COMMENT ON TABLE event_sources IS
    'Реестр подключённых систем мониторинга. kind=canonical — система шлёт готовый '
    'канонический JSON; kind=mapping — преобразование описано в field_map; '
    'kind=adapter — нужен код, потому что преобразование не выражается маппингом.';

COMMENT ON COLUMN event_sources.last_event_at IS
    'Опора для панели «состояние интеграций»: молчащий источник виден сразу.';

INSERT INTO event_sources (code, name, kind, adapter_class, timezone) VALUES
    ('zabbix',     'Zabbix Server 7.0 (АСУ ТП)',   'adapter', 'ZabbixAdapter',     'Europe/Moscow'),
    ('solarwinds', 'SolarWinds HCO (сеть)',        'adapter', 'SolarWindsAdapter', NULL),
    ('generic',    'Универсальный приём',          'canonical', NULL,              NULL);


-- =============================================================================
-- A1. СЫРЫЕ СОБЫТИЯ
-- =============================================================================

CREATE TABLE raw_events (
    id            bigint GENERATED ALWAYS AS IDENTITY,
    received_at   timestamptz NOT NULL DEFAULT now(),  -- время приёма вебхука, UTC
    source        text        NOT NULL,
    payload       jsonb       NOT NULL,                -- тело запроса байт-в-байт
    payload_hash  bytea       NOT NULL,                -- sha256(payload) — защита от ретраев
    processed_at  timestamptz,                         -- NULL = ещё не нормализовано
    process_error text,                                -- текст ошибки нормализации

    CONSTRAINT raw_events_pk PRIMARY KEY (id, received_at),
    CONSTRAINT raw_events_source_fk FOREIGN KEY (source) REFERENCES event_sources(code),
    -- Дедуп ретраев в пределах партиции — повторная доставка приходит
    -- секундами позже оригинала, этого достаточно.
    CONSTRAINT raw_events_dedup_uq UNIQUE (source, payload_hash, received_at)
) PARTITION BY RANGE (received_at);

-- Индекс очереди нормализации: воркер берёт отсюда FOR UPDATE SKIP LOCKED.
CREATE INDEX raw_events_unprocessed_idx ON raw_events (received_at)
    WHERE processed_at IS NULL;

CREATE INDEX raw_events_payload_gin_idx ON raw_events USING gin (payload jsonb_path_ops);

COMMENT ON TABLE raw_events IS
    'Append-only приёмник вебхуков. Обработчик делает ровно один INSERT и отвечает 202 — '
    'никакого парсинга, поэтому приём не может упасть на неожиданном поле. '
    'Ретеншен 30 дней: старые партиции дропаются.';

CREATE TABLE raw_events_2026_08 PARTITION OF raw_events
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE raw_events_2026_09 PARTITION OF raw_events
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');


-- =============================================================================
-- A2. СПРАВОЧНИК НАБЛЮДАЕМЫХ ОБЪЕКТОВ
-- =============================================================================

CREATE TABLE monitored_objects (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source      text NOT NULL,
    external_id text NOT NULL,   -- zabbix: host_technical_name; solarwinds: node.objectId
    name        text NOT NULL,   -- zabbix: host;                solarwinds: node.name
    ip          inet,            -- zabbix: host_ip ('*UNKNOWN*' -> NULL)
    dns         text,
    os          text,            -- solarwinds: node.machineType
    vendor      text,
    location    text,
    contact     text,
    object_group text,           -- zabbix: hostgroup; solarwinds: группа узла
    description text,
    attrs       jsonb       NOT NULL DEFAULT '{}',
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT monitored_objects_source_fk FOREIGN KEY (source) REFERENCES event_sources(code),
    CONSTRAINT monitored_objects_uq UNIQUE (source, external_id)
);

CREATE INDEX monitored_objects_group_idx ON monitored_objects (object_group);

COMMENT ON TABLE monitored_objects IS
    'Хосты и узлы. Дедуплицируются по (source, external_id) — живут дольше инцидентов. '
    'Один физический сервер может присутствовать дважды (из Zabbix и из SolarWinds): '
    'связывание между системами в прототип не входит, см. открытый вопрос 5 в ТЗ.';

COMMENT ON COLUMN monitored_objects.object_group IS
    'Используется правилами отбора тем — по нему темы вроде «БД prod» подбирают события.';


-- =============================================================================
-- A3. ИНЦИДЕНТЫ — агрегат состояния
-- =============================================================================

CREATE TABLE incidents (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source       text   NOT NULL,

    -- Ключ склейки событий одного инцидента:
    --   zabbix     -> event_id ('59')
    --   solarwinds -> alert.objectId || '|' || alert.name ('1042|High CPU Utilization'),
    --                 т.к. objectId — это id УЗЛА, и два разных правила на одном узле
    --                 иначе затрут друг друга
    external_key text NOT NULL,

    -- Ключ дедупликации: sha256(source || object_external_id || rule_key).
    -- В отличие от external_key переживает пересоздание события в источнике.
    fingerprint  bytea NOT NULL,

    object_id    bigint REFERENCES monitored_objects(id),

    rule_key     text,           -- zabbix: trigger_id; solarwinds: alert.name
    rule_name    text NOT NULL,  -- zabbix: trigger_name; solarwinds: alert.name
    rule_expr    text,           -- zabbix: trigger_expression; solarwinds: alert.description

    severity     smallint NOT NULL,  -- каноническая 0..5, см. severity_map
    severity_raw text     NOT NULL,  -- 'High' / 'Critical' — как пришло из источника

    status       text NOT NULL,      -- open | resolved
    opened_at    timestamptz NOT NULL,
    resolved_at  timestamptz,
    duration_s   integer GENERATED ALWAYS AS (
                     EXTRACT(epoch FROM (resolved_at - opened_at))::integer
                 ) STORED,

    -- Дедупликация
    last_event_at    timestamptz NOT NULL,        -- время последнего события инцидента
    last_notified_at timestamptz,                 -- когда по нему в последний раз уведомляли
    dedup_count      integer NOT NULL DEFAULT 1,  -- сколько событий схлопнулось

    -- Квитирование: поля есть, механика в прототипе выключена (FR-FUT-1).
    -- FK на users добавляется ниже, после создания таблицы пользователей.
    acknowledged boolean NOT NULL DEFAULT false,
    ack_by       bigint,
    ack_at       timestamptz,

    url          text,     -- zabbix: trigger_url; solarwinds: alert.detailsUrl
    last_message text,
    tags         text[] NOT NULL DEFAULT '{}',
    is_test      boolean NOT NULL DEFAULT false,
    attrs        jsonb   NOT NULL DEFAULT '{}',

    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT incidents_source_fk    FOREIGN KEY (source) REFERENCES event_sources(code),
    CONSTRAINT incidents_status_chk   CHECK (status IN ('open', 'resolved')),
    CONSTRAINT incidents_severity_chk CHECK (severity BETWEEN 0 AND 5),
    CONSTRAINT incidents_resolved_chk CHECK (status <> 'resolved' OR resolved_at IS NOT NULL),
    CONSTRAINT incidents_uq UNIQUE (source, external_key)
);

CREATE INDEX incidents_open_idx        ON incidents (severity DESC, opened_at DESC)
    WHERE status = 'open';
CREATE INDEX incidents_opened_at_idx   ON incidents (opened_at DESC);
CREATE INDEX incidents_object_idx      ON incidents (object_id, opened_at DESC);
CREATE INDEX incidents_fingerprint_idx ON incidents (fingerprint, status);
CREATE INDEX incidents_tags_gin_idx    ON incidents USING gin (tags);
CREATE INDEX incidents_attrs_gin_idx   ON incidents USING gin (attrs jsonb_path_ops);

COMMENT ON TABLE incidents IS
    'Одна строка = одна проблема. Это ЕДИНСТВЕННАЯ таблица слоя событий, где происходит '
    'UPDATE: событие RESOLVED обновляет строку, созданную событием PROBLEM. Запись идёт '
    'через INSERT ... ON CONFLICT (source, external_key) DO UPDATE, поэтому порядок '
    'доставки вебхуков не важен.';

COMMENT ON COLUMN incidents.last_notified_at IS
    'Опора окна подавления: повтор в пределах SUPPRESS_WINDOW инкрементит dedup_count '
    'и НЕ порождает нового уведомления.';

COMMENT ON COLUMN incidents.attrs IS
    'Всё, что не легло в колонки, но может понадобиться: item_key, item_name, opdata '
    'у Zabbix; alert.message, objectType у SolarWinds. Ничего не выбрасываем.';


-- =============================================================================
-- A4. ЛЕНТА СОБЫТИЙ ИНЦИДЕНТА — append-only
-- =============================================================================

CREATE TABLE incident_events (
    id           bigint GENERATED ALWAYS AS IDENTITY,
    incident_id  bigint NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    raw_event_id bigint NOT NULL,   -- FK не ставим: raw_events партиционирована и чистится

    kind         text NOT NULL,         -- problem | resolved | ack | update
    event_ts     timestamptz NOT NULL,  -- время события В ИСТОЧНИКЕ
    received_at  timestamptz NOT NULL,  -- когда получили мы

    severity     smallint,
    message      text,
    value        text,   -- zabbix: item_lastvalue. У SolarWinds NULL — числа нет отдельным полем
    attrs        jsonb NOT NULL DEFAULT '{}',

    CONSTRAINT incident_events_pk PRIMARY KEY (id, event_ts),
    CONSTRAINT incident_events_kind_chk CHECK (kind IN ('problem', 'resolved', 'ack', 'update'))
) PARTITION BY RANGE (event_ts);

CREATE INDEX incident_events_incident_idx ON incident_events (incident_id, event_ts DESC);

COMMENT ON TABLE incident_events IS
    'Только INSERT, никогда UPDATE. incidents отвечает на вопрос «что сейчас», '
    'incident_events — «как мы к этому пришли».';

CREATE TABLE incident_events_2026_08 PARTITION OF incident_events
    FOR VALUES FROM ('2026-08-01') TO ('2026-09-01');
CREATE TABLE incident_events_2026_09 PARTITION OF incident_events
    FOR VALUES FROM ('2026-09-01') TO ('2026-10-01');


-- =============================================================================
-- B1. ПОЛЬЗОВАТЕЛИ
--
-- ПДн-политика: ФИО, должность и телефон не хранятся. Отображаемое имя
-- подтягивается из каталога по запросу и в БД не пишется.
-- =============================================================================

CREATE TABLE users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Непрозрачный стабильный идентификатор субъекта. Для LDAP — objectGUID,
    -- для локального провайдера — сгенерированный UUID. НЕ логин и НЕ email.
    subject_id    text NOT NULL,
    provider      text NOT NULL,   -- local | ldap

    username      text NOT NULL,   -- логин для входа
    password_hash text,            -- argon2id; NULL для пользователей из каталога

    is_active     boolean NOT NULL DEFAULT true,
    failed_logins smallint NOT NULL DEFAULT 0,
    locked_until  timestamptz,

    roles_synced_at timestamptz,   -- когда последний раз обновляли роли из каталога

    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz,

    CONSTRAINT users_provider_chk CHECK (provider IN ('local', 'ldap')),
    CONSTRAINT users_subject_uq   UNIQUE (provider, subject_id),
    CONSTRAINT users_username_uq  UNIQUE (username),
    -- У локального пользователя пароль обязателен, у доменного его быть не должно
    CONSTRAINT users_password_chk CHECK (
        (provider = 'local' AND password_hash IS NOT NULL) OR
        (provider = 'ldap'  AND password_hash IS NULL)
    )
);

COMMENT ON TABLE users IS
    'Субъекты системы. Осознанно не содержит ПДн: ни ФИО, ни телефона, ни email. '
    'Связка subject_id + подписки остаётся косвенными ПДн, но объём минимален.';

-- Отложенный FK: incidents создаётся раньше users
ALTER TABLE incidents
    ADD CONSTRAINT incidents_ack_by_fk FOREIGN KEY (ack_by) REFERENCES users(id);


-- Контакты — ЕДИНСТВЕННОЕ место в схеме, где лежат ПДн.
-- Существует только пока нет каталога. При переходе на LDAP таблица очищается,
-- и адрес резолвится LdapContactResolver'ом в момент отправки, без хранения.
CREATE TABLE user_contacts (
    user_id    bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel    text   NOT NULL,   -- email | trueconf
    address    text   NOT NULL,
    verified   boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT user_contacts_pk PRIMARY KEY (user_id, channel),
    CONSTRAINT user_contacts_channel_chk CHECK (channel IN ('email', 'trueconf'))
);

COMMENT ON TABLE user_contacts IS
    'ВНИМАНИЕ: содержит персональные данные. Временная таблица на период MVP, пока '
    'нет каталога. Не логировать содержимое. Очищается при переключении на LDAP.';


-- Refresh-сессии. Нужны, чтобы токен можно было ОТОЗВАТЬ: без серверного
-- состояния подписанный refresh живёт до истечения срока, а FR-IDM-4 требует
-- закрывать доступ сразу при отключении учётки в каталоге.
-- Самого токена здесь нет — только его идентификатор (jti).
CREATE TABLE sessions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    jti         text   NOT NULL,          -- идентификатор refresh-токена
    issued_at   timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL,
    revoked_at  timestamptz,              -- отзыв: logout, ротация, деактивация
    replaced_by text,                     -- jti токена, выданного взамен при ротации

    CONSTRAINT sessions_jti_uq UNIQUE (jti)
);

CREATE INDEX sessions_user_idx ON sessions (user_id, revoked_at);

COMMENT ON TABLE sessions IS
    'Активные refresh-сессии. Отзыв — проставление revoked_at: деактивация учётки '
    'закрывает доступ сразу, не дожидаясь истечения токена.';


-- =============================================================================
-- B2. РОЛИ
-- =============================================================================

CREATE TABLE roles (
    code        text PRIMARY KEY,   -- admin | engineer | viewer | dba | ...
    name        text NOT NULL,
    description text,
    is_system   boolean NOT NULL DEFAULT false  -- системные роли нельзя удалить
);

INSERT INTO roles (code, name, description, is_system) VALUES
    ('admin',    'Администратор платформы', 'Темы, подписки, пользователи, аудит', true),
    ('engineer', 'Инженер / дежурный',      'Инциденты, инбокс, свои подписки',    true),
    ('viewer',   'Наблюдатель',             'Только просмотр дашборда',            true);


CREATE TABLE user_roles (
    user_id   bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_code text   NOT NULL REFERENCES roles(code) ON DELETE CASCADE,
    source    text   NOT NULL DEFAULT 'local',  -- local | ldap
    synced_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT user_roles_pk PRIMARY KEY (user_id, role_code),
    CONSTRAINT user_roles_source_chk CHECK (source IN ('local', 'ldap'))
);

COMMENT ON TABLE user_roles IS
    'Роли из каталога — кэш с TTL, перезаписывается при входе. Роли source=local '
    'назначаются администратором и синхронизацией не затрагиваются.';


-- Маппинг групп каталога во внутренние роли. Заполняется администратором,
-- используется LdapIdentityProvider при синхронизации.
CREATE TABLE role_mappings (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_group text NOT NULL,   -- DN группы в AD
    role_code      text NOT NULL REFERENCES roles(code) ON DELETE CASCADE,
    created_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT role_mappings_uq UNIQUE (external_group, role_code)
);


-- =============================================================================
-- C1. КАТАЛОГ ТЕМ
--
-- Тема — единица подписки. Вся сложность отбора событий спрятана в match_rule,
-- поэтому в интерфейсе пользователя фильтров нет: он видит плоский список.
-- =============================================================================

CREATE TABLE topics (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code        text NOT NULL,
    name        text NOT NULL,          -- «БД prod», «Сеть МСК»
    description text,

    -- Правило отбора событий. Пустой объект = тема ловит всё.
    -- {
    --   "sources":       ["zabbix"],            -- любой из; отсутствует = все
    --   "severity_min":  3,                     -- каноническая шкала 0..5
    --   "object_groups": ["prod-db"],           -- monitored_objects.object_group
    --   "tags_any":      ["disk", "prod"],      -- достаточно одного совпадения
    --   "tags_all":      ["prod"]               -- нужны все
    -- }
    match_rule  jsonb NOT NULL DEFAULT '{}',

    is_active   boolean NOT NULL DEFAULT true,
    created_by  bigint REFERENCES users(id),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT topics_code_uq UNIQUE (code)
);

CREATE INDEX topics_active_idx ON topics (is_active) WHERE is_active;
CREATE INDEX topics_rule_gin_idx ON topics USING gin (match_rule jsonb_path_ops);

COMMENT ON COLUMN topics.match_rule IS
    'Условия соединяются логическим И. Внутри списка — ИЛИ. Пустой объект ловит все '
    'события. Проверяется кодом маршрутизатора, не триггером БД.';


-- Привязка темы к роли делает подписку обязательной для носителей роли.
CREATE TABLE topic_roles (
    topic_id   bigint NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    role_code  text   NOT NULL REFERENCES roles(code) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT topic_roles_pk PRIMARY KEY (topic_id, role_code)
);

COMMENT ON TABLE topic_roles IS
    'Обязательные подписки НЕ материализуются строками в subscriptions — они '
    'вычисляются из этой таблицы и user_roles. Иначе пришлось бы пересчитывать '
    'подписки при каждом изменении роли.';


-- =============================================================================
-- C2. ПОДПИСКИ
--
-- Хранятся только явные подписки: назначенные администратором и добровольные.
-- Обязательные вычисляются. Эффективный набор — во вьюхе v_effective_subscriptions.
-- =============================================================================

CREATE TABLE subscriptions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     bigint NOT NULL REFERENCES users(id)  ON DELETE CASCADE,
    topic_id    bigint NOT NULL REFERENCES topics(id) ON DELETE CASCADE,

    origin      text NOT NULL,   -- assigned | self
    channels    text[] NOT NULL DEFAULT '{web}',  -- web | email | trueconf

    granted_by  bigint REFERENCES users(id),      -- кто назначил (для assigned)
    muted_until timestamptz,                      -- временное глушение

    created_at  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT subscriptions_origin_chk CHECK (origin IN ('assigned', 'self')),
    CONSTRAINT subscriptions_uq UNIQUE (user_id, topic_id),
    -- Назначенная подписка обязана знать, кто её назначил
    CONSTRAINT subscriptions_granted_chk CHECK (origin <> 'assigned' OR granted_by IS NOT NULL)
);

CREATE INDEX subscriptions_topic_idx ON subscriptions (topic_id);
CREATE INDEX subscriptions_user_idx  ON subscriptions (user_id);

COMMENT ON TABLE subscriptions IS
    'Одна строка на пару (пользователь, тема). origin=assigned снимает только '
    'администратор, origin=self — сам пользователь. Обязательных подписок здесь нет.';


-- Эффективные подписки: объединение обязательных и явных.
-- Приоритет происхождения: mandatory_role > assigned > self.
CREATE VIEW v_effective_subscriptions AS
WITH mandatory AS (
    SELECT ur.user_id,
           tr.topic_id,
           'mandatory_role'::text AS origin,
           ARRAY['web']::text[]   AS channels,
           NULL::timestamptz      AS muted_until
    FROM user_roles ur
    JOIN topic_roles tr ON tr.role_code = ur.role_code
),
explicit AS (
    SELECT s.user_id,
           s.topic_id,
           s.origin,
           s.channels,
           s.muted_until
    FROM subscriptions s
),
merged AS (
    SELECT * FROM mandatory
    UNION ALL
    SELECT * FROM explicit
)
SELECT DISTINCT ON (m.user_id, m.topic_id)
       m.user_id,
       m.topic_id,
       m.origin,
       m.channels,
       -- Обязательную подписку глушение не отключает
       CASE WHEN m.origin = 'mandatory_role' THEN NULL ELSE m.muted_until END AS muted_until,
       (m.origin = 'mandatory_role' OR m.origin = 'assigned') AS is_mandatory
FROM merged m
JOIN topics t ON t.id = m.topic_id AND t.is_active
JOIN users  u ON u.id = m.user_id  AND u.is_active
ORDER BY m.user_id, m.topic_id,
         CASE m.origin WHEN 'mandatory_role' THEN 1 WHEN 'assigned' THEN 2 ELSE 3 END;

COMMENT ON VIEW v_effective_subscriptions IS
    'Ради этого вся конструкция: маршрутизатор делает один запрос вместо трёх '
    'веток кода на происхождение подписки.';


-- =============================================================================
-- D1. УВЕДОМЛЕНИЯ — они же очередь доставки
--
-- Отдельной таблицы-очереди нет: воркер разбирает pending через
-- FOR UPDATE SKIP LOCKED. Один компонент вместо двух.
-- =============================================================================

CREATE TABLE notifications (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id bigint NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    user_id     bigint NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
    topic_id    bigint REFERENCES topics(id) ON DELETE SET NULL,  -- по какой теме прилетело

    channel     text NOT NULL,   -- web | email | trueconf
    status      text NOT NULL DEFAULT 'pending',  -- pending | sent | failed | skipped

    attempts     smallint    NOT NULL DEFAULT 0,
    next_retry_at timestamptz,
    last_error   text,

    created_at  timestamptz NOT NULL DEFAULT now(),
    sent_at     timestamptz,
    read_at     timestamptz,     -- только для канала web

    CONSTRAINT notifications_channel_chk CHECK (channel IN ('web', 'email', 'trueconf')),
    CONSTRAINT notifications_status_chk  CHECK (status IN ('pending', 'sent', 'failed', 'skipped')),
    -- Один инцидент — одно уведомление на канал пользователю, даже если он
    -- подписан на несколько подходящих тем (FR-DLV-2)
    CONSTRAINT notifications_uq UNIQUE (incident_id, user_id, channel)
);

-- Индекс очереди доставки
CREATE INDEX notifications_pending_idx ON notifications (created_at)
    WHERE status = 'pending';
CREATE INDEX notifications_retry_idx ON notifications (next_retry_at)
    WHERE status = 'pending' AND next_retry_at IS NOT NULL;
-- Индекс инбокса
CREATE INDEX notifications_inbox_idx ON notifications (user_id, created_at DESC)
    WHERE channel = 'web';
CREATE INDEX notifications_unread_idx ON notifications (user_id)
    WHERE channel = 'web' AND read_at IS NULL;

COMMENT ON TABLE notifications IS
    'Экземпляр доставки. Адрес получателя здесь НЕ хранится — резолвится '
    'ContactResolver''ом в момент отправки и нигде не персистится (FR-DLV-4).';

COMMENT ON COLUMN notifications.status IS
    'skipped — подписка была заглушена или сработало окно подавления; строка '
    'сохраняется, чтобы было видно, что решение принято осознанно.';


-- =============================================================================
-- D2. СВОДКИ ЯЗЫКОВОЙ МОДЕЛИ
-- =============================================================================

CREATE TABLE llm_summaries (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    incident_id    bigint NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,

    model          text NOT NULL,       -- имя модели на локальном endpoint
    prompt_version text NOT NULL,       -- версия шаблона промпта
    content        text NOT NULL,       -- человекочитаемая сводка
    masked         boolean NOT NULL DEFAULT false,  -- применялось ли маскирование

    latency_ms     integer,
    created_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT llm_summaries_uq UNIQUE (incident_id, prompt_version)
);

COMMENT ON TABLE llm_summaries IS
    'Модель и версия промпта фиксируются, чтобы результаты были воспроизводимы и '
    'сравнимы между прогонами. Недоступность модели не блокирует доставку.';


-- =============================================================================
-- E1. МАППИНГ SEVERITY
-- Держим в таблице, а не в коде: источники будут добавляться.
-- =============================================================================

CREATE TABLE severity_map (
    source    text     NOT NULL,
    raw_value text     NOT NULL,   -- как пишет источник, регистронезависимо
    severity  smallint NOT NULL,
    label     text     NOT NULL,

    CONSTRAINT severity_map_pk  PRIMARY KEY (source, raw_value),
    CONSTRAINT severity_map_fk  FOREIGN KEY (source) REFERENCES event_sources(code) ON DELETE CASCADE,
    CONSTRAINT severity_map_chk CHECK (severity BETWEEN 0 AND 5)
);

-- Zabbix: маппим по числовому event_nseverity — оно надёжнее строки
INSERT INTO severity_map (source, raw_value, severity, label) VALUES
    ('zabbix', '0', 0, 'Not classified'),
    ('zabbix', '1', 1, 'Information'),
    ('zabbix', '2', 2, 'Warning'),
    ('zabbix', '3', 3, 'Average'),
    ('zabbix', '4', 4, 'High'),
    ('zabbix', '5', 5, 'Disaster'),
    ('solarwinds', 'informational', 0, 'Informational'),
    ('solarwinds', 'notice',        1, 'Notice'),
    ('solarwinds', 'warning',       2, 'Warning'),
    ('solarwinds', 'serious',       3, 'Serious'),
    ('solarwinds', 'critical',      4, 'Critical');

-- ВНИМАНИЕ: набор уровней SolarWinds выписан по памяти. Сверить с реальной
-- шкалой Orion до боевого использования (открытый вопрос 2 в ТЗ).


-- =============================================================================
-- E2. АУДИТ
-- Требование ИБ: изменения тем, ролей и подписок должны быть прослеживаемы.
-- =============================================================================

CREATE TABLE audit_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id    bigint REFERENCES users(id),   -- NULL = системное действие
    action      text NOT NULL,                 -- topic.create | subscription.assign | user.login ...
    entity_type text NOT NULL,                 -- topic | subscription | user | role
    entity_id   text,
    old_value   jsonb,
    new_value   jsonb,
    request_id  text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_log_created_idx ON audit_log (created_at DESC);
CREATE INDEX audit_log_actor_idx   ON audit_log (actor_id, created_at DESC);
CREATE INDEX audit_log_entity_idx  ON audit_log (entity_type, entity_id);

COMMENT ON TABLE audit_log IS
    'Не писать сюда адреса доставки, тела уведомлений и payload событий — '
    'журнал не должен становиться обходным хранилищем ПДн.';


-- =============================================================================
-- ВЬЮХИ
-- =============================================================================

-- Активные инциденты для дашборда. Тестовые исключены (FR-INC-7).
CREATE VIEW v_active_incidents AS
SELECT i.id,
       i.source,
       o.name  AS object_name,
       o.ip,
       o.object_group,
       i.rule_name,
       i.severity,
       i.severity_raw,
       i.opened_at,
       now() - i.opened_at AS age,
       i.dedup_count,
       i.acknowledged,
       i.url,
       i.last_message,
       i.tags
FROM incidents i
LEFT JOIN monitored_objects o ON o.id = i.object_id
WHERE i.status = 'open'
  AND i.is_test = false
ORDER BY i.severity DESC, i.opened_at;


-- Инбокс: непрочитанные веб-уведомления с контекстом инцидента.
CREATE VIEW v_user_inbox AS
SELECT n.id           AS notification_id,
       n.user_id,
       n.created_at,
       n.read_at,
       i.id           AS incident_id,
       i.severity,
       i.rule_name,
       i.status       AS incident_status,
       o.name         AS object_name,
       t.name         AS topic_name,
       s.content      AS summary
FROM notifications n
JOIN incidents i          ON i.id = n.incident_id
LEFT JOIN monitored_objects o ON o.id = i.object_id
LEFT JOIN topics t        ON t.id = n.topic_id
LEFT JOIN llm_summaries s ON s.incident_id = i.id
WHERE n.channel = 'web'
  AND n.status = 'sent';


-- =============================================================================
-- ОЧЕРЕДЬ: как воркеры разбирают работу
-- =============================================================================
--
-- Нормализация:
--   SELECT id, received_at, source, payload
--   FROM raw_events
--   WHERE processed_at IS NULL
--   ORDER BY received_at
--   LIMIT 100
--   FOR UPDATE SKIP LOCKED;
--
-- Доставка:
--   SELECT id, incident_id, user_id, channel
--   FROM notifications
--   WHERE status = 'pending'
--     AND (next_retry_at IS NULL OR next_retry_at <= now())
--   ORDER BY created_at
--   LIMIT 50
--   FOR UPDATE SKIP LOCKED;
--
-- SKIP LOCKED позволяет запускать несколько воркеров без координатора: каждый
-- берёт свою пачку, заблокированные строки пропускаются.


-- =============================================================================
-- ПРИМЕР: как ложатся исходные payload'ы
-- =============================================================================
--
-- monitored_objects:
--   1 | zabbix     | webhook-test-host | webhook-test-host | NULL          | NULL
--   2 | solarwinds | 1042              | WINDOWS-STD3-80   | 83.166.251.15 | Windows Server 2022
--
-- incidents (2 строки, НЕ 3 — два вебхука Zabbix это один инцидент):
--   1 | zabbix     | 59                        | Test webhook trigger | 4 | High     | resolved | 10:53:52Z | 10:54:20Z | 28
--   2 | solarwinds | 1042|High CPU Utilization | High CPU Utilization | 4 | Critical | open     | 12:42:10Z | NULL      | NULL
--
-- incident_events (3 строки — по одной на каждый вебхук):
--   1 | inc 1 | problem  | 10:53:52Z | value=1    | 'Problem: Test webhook trigger...'
--   2 | inc 1 | resolved | 10:54:20Z | value=0    | 'Resolved: Test webhook trigger'
--   3 | inc 2 | problem  | 12:42:10Z | value=NULL | 'Node WINDOWS-STD3-80 CPU utilization is critical (94%)'


-- =============================================================================
-- ОТКРЫТЫЕ ВОПРОСЫ К МОДЕЛИ ДАННЫХ
-- =============================================================================
--
-- 1. RESET-СОБЫТИЕ SOLARWINDS. Есть только пример с eventType=trigger. Если в
--    reset нет того же objectId — external_key придётся строить иначе.
--    БЛОКИРУЮЩИЙ для склейки инцидентов SolarWinds.
--
-- 2. ТАЙМЗОНА ZABBIX. timestamp/date/time приходят без таймзоны — это локальное
--    время сервера Zabbix. Задаётся конфигурацией ZABBIX_TZ. Ошибка здесь
--    смещает все инциденты на N часов.
--
-- 3. ACK-СОБЫТИЯ. kind='ack' пока гипотетический — реального payload с
--    подтверждением не видели.
--
-- 4. СВЯЗЫВАНИЕ ОБЪЕКТОВ МЕЖДУ СИСТЕМАМИ. Один физический сервер, видимый и
--    Zabbix, и SolarWinds, — две строки в monitored_objects. Понадобится ли
--    слой assets с маппингом, зависит от того, есть ли пересечение по хостам.
--
-- 5. МЕТРИКИ-ТАЙМСЕРИИ. Схема рассчитана ТОЛЬКО на события и алерты. Хранение
--    сырых показателей (CPU/RAM по времени) — отдельная таблица и, вероятно,
--    TimescaleDB. Для потока алертов это оверкилл.
--
-- 6. ЗАПИСЬ СИНХРОННО ИЛИ ЧЕРЕЗ ОЧЕРЕДЬ. Нормализация идёт одной транзакцией:
--    upsert incidents + insert incident_events + update processed_at. На потоке
--    десятков событий в минуту этого достаточно. processed_at оставлен для
--    режима пересчёта из raw при исправлении маппинга.
