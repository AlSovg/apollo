"""Слой A: реестр источников и события.

event_sources → raw_events → monitored_objects → incidents → incident_events.

raw_events и incident_events партиционированы по месяцам и append-only.
incidents — единственная таблица слоя, где происходит UPDATE.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    PrimaryKeyConstraint,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class EventSource(Base):
    """Реестр подключённых систем мониторинга: новый источник = строка здесь."""

    __tablename__ = "event_sources"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('adapter', 'mapping', 'canonical')",
            name="event_sources_kind_chk",
        ),
        # Адаптерный источник обязан указать класс, маппинговый — непустой маппинг
        CheckConstraint(
            "(kind = 'adapter'   AND adapter_class IS NOT NULL) OR "
            "(kind = 'mapping'   AND field_map <> '{}') OR "
            "(kind = 'canonical')",
            name="event_sources_impl_chk",
        ),
        {
            "comment": (
                "Реестр подключённых систем мониторинга. kind=canonical — система шлёт "
                "готовый канонический JSON; kind=mapping — преобразование описано в "
                "field_map; kind=adapter — нужен код, потому что преобразование не "
                "выражается маппингом."
            )
        },
    )

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)

    adapter_class: Mapped[str | None] = mapped_column(Text)
    field_map: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )

    token_hash: Mapped[bytes | None] = mapped_column(LargeBinary)
    hmac_secret: Mapped[bytes | None] = mapped_column(LargeBinary)
    ip_allowlist: Mapped[list[str]] = mapped_column(
        ARRAY(INET), nullable=False, server_default=text("'{}'")
    )

    timezone: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )

    last_event_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment="Опора для панели «состояние интеграций»: молчащий источник виден сразу.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RawEvent(Base):
    """Сырой вебхук как пришёл. Партиционирована по received_at, ретеншен 30 дней."""

    __tablename__ = "raw_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", "received_at", name="raw_events_pk"),
        # Дедуп ретраев в пределах партиции — повторная доставка приходит
        # секундами позже оригинала, этого достаточно.
        UniqueConstraint(
            "source", "payload_hash", "received_at", name="raw_events_dedup_uq"
        ),
        # Индекс очереди нормализации: воркер берёт отсюда FOR UPDATE SKIP LOCKED
        Index(
            "raw_events_unprocessed_idx",
            "received_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
        Index(
            "raw_events_payload_gin_idx",
            "payload",
            postgresql_using="gin",
            postgresql_ops={"payload": "jsonb_path_ops"},
        ),
        {
            "postgresql_partition_by": "RANGE (received_at)",
            "comment": (
                "Append-only приёмник вебхуков. Обработчик делает ровно один INSERT и "
                "отвечает 202 — никакого парсинга, поэтому приём не может упасть на "
                "неожиданном поле. Ретеншен 30 дней: старые партиции дропаются."
            ),
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        server_default=func.now(),
        comment="Время приёма вебхука, UTC",
    )
    source: Mapped[str] = mapped_column(
        Text, ForeignKey("event_sources.code", name="raw_events_source_fk"), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="Тело запроса байт-в-байт"
    )
    payload_hash: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, comment="sha256(payload) — защита от ретраев"
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="NULL = ещё не нормализовано"
    )
    process_error: Mapped[str | None] = mapped_column(
        Text, comment="Текст ошибки нормализации"
    )


class MonitoredObject(Base):
    """Наблюдаемый объект: хост или узел. Живёт дольше инцидентов."""

    __tablename__ = "monitored_objects"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="monitored_objects_uq"),
        Index("monitored_objects_group_idx", "object_group"),
        {
            "comment": (
                "Хосты и узлы. Дедуплицируются по (source, external_id) — живут дольше "
                "инцидентов. Один физический сервер может присутствовать дважды (из "
                "Zabbix и из SolarWinds): связывание между системами в прототип не "
                "входит, см. открытый вопрос 5 в ТЗ."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(
        Text,
        ForeignKey("event_sources.code", name="monitored_objects_source_fk"),
        nullable=False,
    )
    external_id: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="zabbix: host_technical_name; solarwinds: node.objectId",
    )
    name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="zabbix: host; solarwinds: node.name"
    )
    ip: Mapped[str | None] = mapped_column(
        INET, comment="zabbix: host_ip ('*UNKNOWN*' -> NULL)"
    )
    dns: Mapped[str | None] = mapped_column(Text)
    os: Mapped[str | None] = mapped_column(Text, comment="solarwinds: node.machineType")
    vendor: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    contact: Mapped[str | None] = mapped_column(Text)
    object_group: Mapped[str | None] = mapped_column(
        Text,
        comment=(
            "Используется правилами отбора тем — по нему темы вроде «БД prod» "
            "подбирают события."
        ),
    )
    description: Mapped[str | None] = mapped_column(Text)
    attrs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Incident(Base):
    """Агрегат состояния: одна строка = одна проблема."""

    __tablename__ = "incidents"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'resolved')", name="incidents_status_chk"),
        CheckConstraint("severity BETWEEN 0 AND 5", name="incidents_severity_chk"),
        CheckConstraint(
            "status <> 'resolved' OR resolved_at IS NOT NULL",
            name="incidents_resolved_chk",
        ),
        UniqueConstraint("source", "external_key", name="incidents_uq"),
        Index(
            "incidents_open_idx",
            text("severity DESC"),
            text("opened_at DESC"),
            postgresql_where=text("status = 'open'::text"),
        ),
        Index("incidents_opened_at_idx", text("opened_at DESC")),
        Index("incidents_object_idx", "object_id", text("opened_at DESC")),
        Index("incidents_fingerprint_idx", "fingerprint", "status"),
        Index("incidents_tags_gin_idx", "tags", postgresql_using="gin"),
        Index(
            "incidents_attrs_gin_idx",
            "attrs",
            postgresql_using="gin",
            postgresql_ops={"attrs": "jsonb_path_ops"},
        ),
        {
            "comment": (
                "Одна строка = одна проблема. Это ЕДИНСТВЕННАЯ таблица слоя событий, где "
                "происходит UPDATE: событие RESOLVED обновляет строку, созданную событием "
                "PROBLEM. Запись идёт через INSERT ... ON CONFLICT (source, external_key) "
                "DO UPDATE, поэтому порядок доставки вебхуков не важен."
            )
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    source: Mapped[str] = mapped_column(
        Text, ForeignKey("event_sources.code", name="incidents_source_fk"), nullable=False
    )

    external_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment=(
            "Ключ склейки событий одного инцидента: zabbix -> event_id; "
            "solarwinds -> alert.objectId || '|' || alert.name, т.к. objectId — это id "
            "УЗЛА, и два разных правила на одном узле иначе затрут друг друга."
        ),
    )
    fingerprint: Mapped[bytes] = mapped_column(
        LargeBinary,
        nullable=False,
        comment=(
            "sha256(source || object_external_id || rule_key). В отличие от external_key "
            "переживает пересоздание события в источнике."
        ),
    )

    object_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("monitored_objects.id")
    )

    rule_key: Mapped[str | None] = mapped_column(
        Text, comment="zabbix: trigger_id; solarwinds: alert.name"
    )
    rule_name: Mapped[str] = mapped_column(
        Text, nullable=False, comment="zabbix: trigger_name; solarwinds: alert.name"
    )
    rule_expr: Mapped[str | None] = mapped_column(
        Text, comment="zabbix: trigger_expression; solarwinds: alert.description"
    )

    severity: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="Каноническая 0..5, см. severity_map"
    )
    severity_raw: Mapped[str] = mapped_column(
        Text, nullable=False, comment="'High' / 'Critical' — как пришло из источника"
    )

    status: Mapped[str] = mapped_column(Text, nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[int | None] = mapped_column(
        Integer,
        Computed("EXTRACT(epoch FROM (resolved_at - opened_at))::integer", persisted=True),
    )

    last_event_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Время последнего события инцидента"
    )
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        comment=(
            "Опора окна подавления: повтор в пределах SUPPRESS_WINDOW инкрементит "
            "dedup_count и НЕ порождает нового уведомления."
        ),
    )
    dedup_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
        comment="Сколько событий схлопнулось",
    )

    # Квитирование: поля есть, механика в прототипе выключена (FR-FUT-1)
    acknowledged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    ack_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", name="incidents_ack_by_fk")
    )
    ack_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    url: Mapped[str | None] = mapped_column(
        Text, comment="zabbix: trigger_url; solarwinds: alert.detailsUrl"
    )
    last_message: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default=text("'{}'")
    )
    is_test: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    attrs: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'"),
        comment=(
            "Всё, что не легло в колонки, но может понадобиться: item_key, item_name, "
            "opdata у Zabbix; alert.message, objectType у SolarWinds. Ничего не выбрасываем."
        ),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class IncidentEvent(Base):
    """Лента инцидента. Только INSERT, партиционирована по event_ts."""

    __tablename__ = "incident_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", "event_ts", name="incident_events_pk"),
        CheckConstraint(
            "kind IN ('problem', 'resolved', 'ack', 'update')",
            name="incident_events_kind_chk",
        ),
        Index("incident_events_incident_idx", "incident_id", text("event_ts DESC")),
        {
            "postgresql_partition_by": "RANGE (event_ts)",
            "comment": (
                "Только INSERT, никогда UPDATE. incidents отвечает на вопрос «что сейчас», "
                "incident_events — «как мы к этому пришли»."
            ),
        },
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    incident_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False
    )
    raw_event_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="FK не ставим: raw_events партиционирована и чистится",
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    event_ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        comment="Время события В ИСТОЧНИКЕ",
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="Когда получили мы"
    )

    severity: Mapped[int | None] = mapped_column(SmallInteger)
    message: Mapped[str | None] = mapped_column(Text)
    value: Mapped[str | None] = mapped_column(
        Text,
        comment="zabbix: item_lastvalue. У SolarWinds NULL — числа нет отдельным полем",
    )
    attrs: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'")
    )
