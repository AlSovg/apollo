"""Начальная схема: события, субъекты, подписки, доставка, служебное.

Перенос db/schema.sql версии 2.1. Партиционирование, партиционные индексы и
вьюхи создаются явным SQL: ORM их не выражает.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-10

"""

from collections.abc import Sequence
from datetime import date

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.services.partitions import partition_ddl_batch

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Партиционированные таблицы: DDL как в db/schema.sql --------------------

RAW_EVENTS_DDL = """
CREATE TABLE raw_events (
    id            bigint GENERATED ALWAYS AS IDENTITY,
    received_at   timestamptz NOT NULL DEFAULT now(),
    source        text        NOT NULL,
    payload       jsonb       NOT NULL,
    payload_hash  bytea       NOT NULL,
    processed_at  timestamptz,
    process_error text,

    CONSTRAINT raw_events_pk PRIMARY KEY (id, received_at),
    CONSTRAINT raw_events_source_fk FOREIGN KEY (source) REFERENCES event_sources(code),
    CONSTRAINT raw_events_dedup_uq UNIQUE (source, payload_hash, received_at)
) PARTITION BY RANGE (received_at)
"""

INCIDENT_EVENTS_DDL = """
CREATE TABLE incident_events (
    id           bigint GENERATED ALWAYS AS IDENTITY,
    incident_id  bigint NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    raw_event_id bigint NOT NULL,

    kind         text NOT NULL,
    event_ts     timestamptz NOT NULL,
    received_at  timestamptz NOT NULL,

    severity     smallint,
    message      text,
    value        text,
    attrs        jsonb NOT NULL DEFAULT '{}',

    CONSTRAINT incident_events_pk PRIMARY KEY (id, event_ts),
    CONSTRAINT incident_events_kind_chk CHECK (kind IN ('problem', 'resolved', 'ack', 'update'))
) PARTITION BY RANGE (event_ts)
"""

PARTITIONED_COMMENTS = [
    """COMMENT ON TABLE raw_events IS
        'Append-only приёмник вебхуков. Обработчик делает ровно один INSERT и отвечает 202 — '
        'никакого парсинга, поэтому приём не может упасть на неожиданном поле. '
        'Ретеншен 30 дней: старые партиции дропаются.'""",
    "COMMENT ON COLUMN raw_events.received_at IS 'Время приёма вебхука, UTC'",
    "COMMENT ON COLUMN raw_events.payload IS 'Тело запроса байт-в-байт'",
    "COMMENT ON COLUMN raw_events.payload_hash IS 'sha256(payload) — защита от ретраев'",
    "COMMENT ON COLUMN raw_events.processed_at IS 'NULL = ещё не нормализовано'",
    "COMMENT ON COLUMN raw_events.process_error IS 'Текст ошибки нормализации'",
    """COMMENT ON TABLE incident_events IS
        'Только INSERT, никогда UPDATE. incidents отвечает на вопрос «что сейчас», '
        'incident_events — «как мы к этому пришли».'""",
    """COMMENT ON COLUMN incident_events.raw_event_id IS
        'FK не ставим: raw_events партиционирована и чистится'""",
    "COMMENT ON COLUMN incident_events.event_ts IS 'Время события В ИСТОЧНИКЕ'",
    "COMMENT ON COLUMN incident_events.received_at IS 'Когда получили мы'",
    """COMMENT ON COLUMN incident_events.value IS
        'zabbix: item_lastvalue. У SolarWinds NULL — числа нет отдельным полем'""",
]

# Индексы с частичными условиями и GIN — только явным SQL
INDEXES_DDL = [
    # Индекс очереди нормализации: воркер берёт отсюда FOR UPDATE SKIP LOCKED
    """CREATE INDEX raw_events_unprocessed_idx ON raw_events (received_at)
       WHERE processed_at IS NULL""",
    "CREATE INDEX raw_events_payload_gin_idx ON raw_events USING gin (payload jsonb_path_ops)",
    "CREATE INDEX monitored_objects_group_idx ON monitored_objects (object_group)",
    """CREATE INDEX incidents_open_idx ON incidents (severity DESC, opened_at DESC)
       WHERE status = 'open'""",
    "CREATE INDEX incidents_opened_at_idx ON incidents (opened_at DESC)",
    "CREATE INDEX incidents_object_idx ON incidents (object_id, opened_at DESC)",
    "CREATE INDEX incidents_fingerprint_idx ON incidents (fingerprint, status)",
    "CREATE INDEX incidents_tags_gin_idx ON incidents USING gin (tags)",
    "CREATE INDEX incidents_attrs_gin_idx ON incidents USING gin (attrs jsonb_path_ops)",
    "CREATE INDEX incident_events_incident_idx ON incident_events (incident_id, event_ts DESC)",
    "CREATE INDEX topics_active_idx ON topics (is_active) WHERE is_active",
    "CREATE INDEX topics_rule_gin_idx ON topics USING gin (match_rule jsonb_path_ops)",
    "CREATE INDEX subscriptions_topic_idx ON subscriptions (topic_id)",
    "CREATE INDEX subscriptions_user_idx ON subscriptions (user_id)",
    # Очередь доставки
    "CREATE INDEX notifications_pending_idx ON notifications (created_at) WHERE status = 'pending'",
    """CREATE INDEX notifications_retry_idx ON notifications (next_retry_at)
       WHERE status = 'pending' AND next_retry_at IS NOT NULL""",
    # Инбокс
    """CREATE INDEX notifications_inbox_idx ON notifications (user_id, created_at DESC)
       WHERE channel = 'web'""",
    """CREATE INDEX notifications_unread_idx ON notifications (user_id)
       WHERE channel = 'web' AND read_at IS NULL""",
    "CREATE INDEX audit_log_created_idx ON audit_log (created_at DESC)",
    "CREATE INDEX audit_log_actor_idx ON audit_log (actor_id, created_at DESC)",
    "CREATE INDEX audit_log_entity_idx ON audit_log (entity_type, entity_id)",
]

# --- Вьюхи -----------------------------------------------------------------

V_EFFECTIVE_SUBSCRIPTIONS = """
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
       CASE WHEN m.origin = 'mandatory_role' THEN NULL ELSE m.muted_until END AS muted_until,
       (m.origin = 'mandatory_role' OR m.origin = 'assigned') AS is_mandatory
FROM merged m
JOIN topics t ON t.id = m.topic_id AND t.is_active
JOIN users  u ON u.id = m.user_id  AND u.is_active
ORDER BY m.user_id, m.topic_id,
         CASE m.origin WHEN 'mandatory_role' THEN 1 WHEN 'assigned' THEN 2 ELSE 3 END
"""

V_ACTIVE_INCIDENTS = """
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
ORDER BY i.severity DESC, i.opened_at
"""

V_USER_INBOX = """
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
  AND n.status = 'sent'
"""

VIEW_COMMENTS = [
    """COMMENT ON VIEW v_effective_subscriptions IS
        'Ради этого вся конструкция: маршрутизатор делает один запрос вместо трёх '
        'веток кода на происхождение подписки.'""",
]


def upgrade() -> None:
    # --- A0. Реестр источников --------------------------------------------
    op.create_table(
        "event_sources",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("adapter_class", sa.Text(), nullable=True),
        sa.Column(
            "field_map",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.LargeBinary(), nullable=True),
        sa.Column("hmac_secret", sa.LargeBinary(), nullable=True),
        sa.Column(
            "ip_allowlist",
            postgresql.ARRAY(postgresql.INET()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("timezone", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "last_event_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Опора для панели «состояние интеграций»: молчащий источник виден сразу.",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('adapter', 'mapping', 'canonical')", name="event_sources_kind_chk"
        ),
        sa.CheckConstraint(
            "(kind = 'adapter'   AND adapter_class IS NOT NULL) OR "
            "(kind = 'mapping'   AND field_map <> '{}') OR "
            "(kind = 'canonical')",
            name="event_sources_impl_chk",
        ),
        sa.PrimaryKeyConstraint("code"),
        comment=(
            "Реестр подключённых систем мониторинга. kind=canonical — система шлёт готовый "
            "канонический JSON; kind=mapping — преобразование описано в field_map; "
            "kind=adapter — нужен код, потому что преобразование не выражается маппингом."
        ),
    )

    # --- A1. Сырые события (партиционирована) ------------------------------
    op.execute(RAW_EVENTS_DDL)

    # --- A2. Наблюдаемые объекты -------------------------------------------
    op.create_table(
        "monitored_objects",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "external_id",
            sa.Text(),
            nullable=False,
            comment="zabbix: host_technical_name; solarwinds: node.objectId",
        ),
        sa.Column(
            "name", sa.Text(), nullable=False, comment="zabbix: host; solarwinds: node.name"
        ),
        sa.Column(
            "ip", postgresql.INET(), nullable=True, comment="zabbix: host_ip ('*UNKNOWN*' -> NULL)"
        ),
        sa.Column("dns", sa.Text(), nullable=True),
        sa.Column("os", sa.Text(), nullable=True, comment="solarwinds: node.machineType"),
        sa.Column("vendor", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("contact", sa.Text(), nullable=True),
        sa.Column(
            "object_group",
            sa.Text(),
            nullable=True,
            comment=(
                "Используется правилами отбора тем — по нему темы вроде «БД prod» "
                "подбирают события."
            ),
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "attrs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "first_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["source"], ["event_sources.code"], name="monitored_objects_source_fk"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_id", name="monitored_objects_uq"),
        comment=(
            "Хосты и узлы. Дедуплицируются по (source, external_id) — живут дольше инцидентов. "
            "Один физический сервер может присутствовать дважды (из Zabbix и из SolarWinds): "
            "связывание между системами в прототип не входит, см. открытый вопрос 5 в ТЗ."
        ),
    )

    # --- B. Субъекты и роли (нужны раньше incidents из-за ack_by) -----------
    op.create_table(
        "roles",
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "is_system",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Системные роли нельзя удалить",
        ),
        sa.PrimaryKeyConstraint("code"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column(
            "subject_id",
            sa.Text(),
            nullable=False,
            comment=(
                "Непрозрачный стабильный идентификатор субъекта. Для LDAP — objectGUID, "
                "для локального провайдера — сгенерированный UUID. НЕ логин и НЕ email."
            ),
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False, comment="Логин для входа"),
        sa.Column(
            "password_hash",
            sa.Text(),
            nullable=True,
            comment="argon2id; NULL для пользователей из каталога",
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "failed_logins", sa.SmallInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "roles_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Когда последний раз обновляли роли из каталога",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("provider IN ('local', 'ldap')", name="users_provider_chk"),
        sa.CheckConstraint(
            "(provider = 'local' AND password_hash IS NOT NULL) OR "
            "(provider = 'ldap'  AND password_hash IS NULL)",
            name="users_password_chk",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "subject_id", name="users_subject_uq"),
        sa.UniqueConstraint("username", name="users_username_uq"),
        comment=(
            "Субъекты системы. Осознанно не содержит ПДн: ни ФИО, ни телефона, ни email. "
            "Связка subject_id + подписки остаётся косвенными ПДн, но объём минимален."
        ),
    )

    op.create_table(
        "user_contacts",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("address", sa.Text(), nullable=False),
        sa.Column("verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "channel IN ('email', 'trueconf')", name="user_contacts_channel_chk"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "channel", name="user_contacts_pk"),
        comment=(
            "ВНИМАНИЕ: содержит персональные данные. Временная таблица на период MVP, пока "
            "нет каталога. Не логировать содержимое. Очищается при переключении на LDAP."
        ),
    )

    op.create_table(
        "user_roles",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("role_code", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), server_default=sa.text("'local'"), nullable=False),
        sa.Column(
            "synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("source IN ('local', 'ldap')", name="user_roles_source_chk"),
        sa.ForeignKeyConstraint(["role_code"], ["roles.code"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_code", name="user_roles_pk"),
        comment=(
            "Роли из каталога — кэш с TTL, перезаписывается при входе. Роли source=local "
            "назначаются администратором и синхронизацией не затрагиваются."
        ),
    )

    op.create_table(
        "role_mappings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("external_group", sa.Text(), nullable=False, comment="DN группы в AD"),
        sa.Column("role_code", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["role_code"], ["roles.code"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_group", "role_code", name="role_mappings_uq"),
    )

    # --- A3. Инциденты ------------------------------------------------------
    op.create_table(
        "incidents",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "external_key",
            sa.Text(),
            nullable=False,
            comment=(
                "Ключ склейки событий одного инцидента: zabbix -> event_id; "
                "solarwinds -> alert.objectId || '|' || alert.name, т.к. objectId — это id "
                "УЗЛА, и два разных правила на одном узле иначе затрут друг друга."
            ),
        ),
        sa.Column(
            "fingerprint",
            sa.LargeBinary(),
            nullable=False,
            comment=(
                "sha256(source || object_external_id || rule_key). В отличие от external_key "
                "переживает пересоздание события в источнике."
            ),
        ),
        sa.Column("object_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "rule_key", sa.Text(), nullable=True, comment="zabbix: trigger_id; solarwinds: alert.name"
        ),
        sa.Column(
            "rule_name",
            sa.Text(),
            nullable=False,
            comment="zabbix: trigger_name; solarwinds: alert.name",
        ),
        sa.Column(
            "rule_expr",
            sa.Text(),
            nullable=True,
            comment="zabbix: trigger_expression; solarwinds: alert.description",
        ),
        sa.Column(
            "severity", sa.SmallInteger(), nullable=False, comment="Каноническая 0..5, см. severity_map"
        ),
        sa.Column(
            "severity_raw",
            sa.Text(),
            nullable=False,
            comment="'High' / 'Critical' — как пришло из источника",
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "duration_s",
            sa.Integer(),
            sa.Computed("EXTRACT(epoch FROM (resolved_at - opened_at))::integer", persisted=True),
            nullable=True,
        ),
        sa.Column(
            "last_event_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Время последнего события инцидента",
        ),
        sa.Column(
            "last_notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "Опора окна подавления: повтор в пределах SUPPRESS_WINDOW инкрементит "
                "dedup_count и НЕ порождает нового уведомления."
            ),
        ),
        sa.Column(
            "dedup_count",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="Сколько событий схлопнулось",
        ),
        sa.Column("acknowledged", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("ack_by", sa.BigInteger(), nullable=True),
        sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "url", sa.Text(), nullable=True, comment="zabbix: trigger_url; solarwinds: alert.detailsUrl"
        ),
        sa.Column("last_message", sa.Text(), nullable=True),
        sa.Column(
            "tags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("is_test", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "attrs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
            comment=(
                "Всё, что не легло в колонки, но может понадобиться: item_key, item_name, "
                "opdata у Zabbix; alert.message, objectType у SolarWinds. Ничего не выбрасываем."
            ),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("severity BETWEEN 0 AND 5", name="incidents_severity_chk"),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="incidents_status_chk"),
        sa.CheckConstraint(
            "status <> 'resolved' OR resolved_at IS NOT NULL", name="incidents_resolved_chk"
        ),
        sa.ForeignKeyConstraint(["ack_by"], ["users.id"], name="incidents_ack_by_fk"),
        sa.ForeignKeyConstraint(["object_id"], ["monitored_objects.id"]),
        sa.ForeignKeyConstraint(["source"], ["event_sources.code"], name="incidents_source_fk"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "external_key", name="incidents_uq"),
        comment=(
            "Одна строка = одна проблема. Это ЕДИНСТВЕННАЯ таблица слоя событий, где происходит "
            "UPDATE: событие RESOLVED обновляет строку, созданную событием PROBLEM. Запись идёт "
            "через INSERT ... ON CONFLICT (source, external_key) DO UPDATE, поэтому порядок "
            "доставки вебхуков не важен."
        ),
    )

    # --- A4. Лента инцидента (партиционирована) ----------------------------
    op.execute(INCIDENT_EVENTS_DDL)

    # --- C. Темы и подписки -------------------------------------------------
    op.create_table(
        "topics",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, comment="«БД prod», «Сеть МСК»"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "match_rule",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
            comment=(
                "Условия соединяются логическим И. Внутри списка — ИЛИ. Пустой объект ловит все "
                "события. Проверяется кодом маршрутизатора, не триггером БД."
            ),
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="topics_code_uq"),
    )

    op.create_table(
        "topic_roles",
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("role_code", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["role_code"], ["roles.code"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("topic_id", "role_code", name="topic_roles_pk"),
        comment=(
            "Обязательные подписки НЕ материализуются строками в subscriptions — они "
            "вычисляются из этой таблицы и user_roles. Иначе пришлось бы пересчитывать "
            "подписки при каждом изменении роли."
        ),
    )

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column(
            "channels",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{web}'"),
            nullable=False,
            comment="web | email | trueconf",
        ),
        sa.Column(
            "granted_by", sa.BigInteger(), nullable=True, comment="Кто назначил (для assigned)"
        ),
        sa.Column(
            "muted_until", sa.DateTime(timezone=True), nullable=True, comment="Временное глушение"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("origin IN ('assigned', 'self')", name="subscriptions_origin_chk"),
        sa.CheckConstraint(
            "origin <> 'assigned' OR granted_by IS NOT NULL", name="subscriptions_granted_chk"
        ),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "topic_id", name="subscriptions_uq"),
        comment=(
            "Одна строка на пару (пользователь, тема). origin=assigned снимает только "
            "администратор, origin=self — сам пользователь. Обязательных подписок здесь нет."
        ),
    )

    # --- D. Доставка --------------------------------------------------------
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("incident_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "topic_id", sa.BigInteger(), nullable=True, comment="По какой теме прилетело"
        ),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            server_default=sa.text("'pending'"),
            nullable=False,
            comment=(
                "skipped — подписка была заглушена или сработало окно подавления; строка "
                "сохраняется, чтобы было видно, что решение принято осознанно."
            ),
        ),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "read_at", sa.DateTime(timezone=True), nullable=True, comment="Только для канала web"
        ),
        sa.CheckConstraint(
            "channel IN ('web', 'email', 'trueconf')", name="notifications_channel_chk"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')", name="notifications_status_chk"
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["topic_id"], ["topics.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", "user_id", "channel", name="notifications_uq"),
        comment=(
            "Экземпляр доставки. Адрес получателя здесь НЕ хранится — резолвится "
            "ContactResolver'ом в момент отправки и нигде не персистится (FR-DLV-4)."
        ),
    )

    op.create_table(
        "llm_summaries",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("incident_id", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False, comment="Имя модели на локальном endpoint"),
        sa.Column("prompt_version", sa.Text(), nullable=False, comment="Версия шаблона промпта"),
        sa.Column("content", sa.Text(), nullable=False, comment="Человекочитаемая сводка"),
        sa.Column(
            "masked",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Применялось ли маскирование",
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", "prompt_version", name="llm_summaries_uq"),
        comment=(
            "Модель и версия промпта фиксируются, чтобы результаты были воспроизводимы и "
            "сравнимы между прогонами. Недоступность модели не блокирует доставку."
        ),
    )

    # --- E. Служебное -------------------------------------------------------
    op.create_table(
        "severity_map",
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "raw_value", sa.Text(), nullable=False, comment="Как пишет источник, регистронезависимо"
        ),
        sa.Column("severity", sa.SmallInteger(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.CheckConstraint("severity BETWEEN 0 AND 5", name="severity_map_chk"),
        sa.ForeignKeyConstraint(
            ["source"], ["event_sources.code"], name="severity_map_fk", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("source", "raw_value", name="severity_map_pk"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True, comment="NULL = системное действие"),
        sa.Column(
            "action",
            sa.Text(),
            nullable=False,
            comment="topic.create | subscription.assign | user.login ...",
        ),
        sa.Column(
            "entity_type", sa.Text(), nullable=False, comment="topic | subscription | user | role"
        ),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        comment=(
            "Не писать сюда адреса доставки, тела уведомлений и payload событий — "
            "журнал не должен становиться обходным хранилищем ПДн."
        ),
    )

    # --- Комментарии партиционированных таблиц, индексы, партиции ----------
    for statement in PARTITIONED_COMMENTS:
        op.execute(statement)
    for statement in INDEXES_DDL:
        op.execute(statement)
    for statement in partition_ddl_batch(date.today(), months_ahead=1):
        op.execute(statement)

    # --- Вьюхи --------------------------------------------------------------
    op.execute(V_EFFECTIVE_SUBSCRIPTIONS)
    op.execute(V_ACTIVE_INCIDENTS)
    op.execute(V_USER_INBOX)
    for statement in VIEW_COMMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_user_inbox")
    op.execute("DROP VIEW IF EXISTS v_active_incidents")
    op.execute("DROP VIEW IF EXISTS v_effective_subscriptions")

    op.drop_table("audit_log")
    op.drop_table("severity_map")
    op.drop_table("llm_summaries")
    op.drop_table("notifications")
    op.drop_table("subscriptions")
    op.drop_table("topic_roles")
    op.drop_table("topics")
    # Партиции удаляются вместе с родительской таблицей
    op.execute("DROP TABLE IF EXISTS incident_events")
    op.drop_table("incidents")
    op.drop_table("role_mappings")
    op.drop_table("user_roles")
    op.drop_table("user_contacts")
    op.drop_table("users")
    op.drop_table("roles")
    op.drop_table("monitored_objects")
    op.execute("DROP TABLE IF EXISTS raw_events")
    op.drop_table("event_sources")
