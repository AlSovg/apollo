"""Реестр источников: подключение системы мониторинга силами администратора.

Это и есть требование кейса о масштабировании без изменения ядра: новая
система заводится записью здесь, а не задачей разработчику. Кнопка «тест»
показывает, во что превратится присланный payload, поэтому маппинг
настраивается за минуты, а не подбором вслепую.
"""

import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.sources import (
    AdapterError,
    UnknownAdapterError,
    adapter_for_source,
    resolve_timezone,
    validate_field_map,
)
from app.api.deps import CurrentUser, require_roles
from app.api.errors import ApiError
from app.config import get_settings
from app.db import get_session
from app.models.events import EventSource
from app.schemas.sources import (
    SourceCreate,
    SourceResponse,
    SourceTestRequest,
    SourceTestResult,
    SourceUpdate,
    TokenIssued,
)
from app.services.audit import write_audit
from app.services.ingest import token_hash
from app.services.normalizer import load_severity_map

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/sources", tags=["admin"])


def _to_response(source: EventSource) -> SourceResponse:
    return SourceResponse(
        code=source.code,
        name=source.name,
        kind=source.kind,
        adapter_class=source.adapter_class,
        field_map=dict(source.field_map or {}),
        timezone=source.timezone,
        ip_allowlist=[str(item) for item in source.ip_allowlist],
        is_active=source.is_active,
        token_set=source.token_hash is not None,
        hmac_set=source.hmac_secret is not None,
        last_event_at=source.last_event_at,
        created_at=source.created_at,
    )


async def _get_source(session: AsyncSession, code: str) -> EventSource:
    source = await session.get(EventSource, code)
    if source is None:
        raise ApiError(404, "SOURCE_NOT_FOUND", "Источник не найден")
    return source


def _check_consistency(kind: str, adapter_class: str | None, field_map: dict[str, Any]) -> None:
    """Те же условия, что и в ограничении event_sources_impl_chk."""
    if kind == "adapter" and not adapter_class:
        raise ApiError(
            400, "ADAPTER_CLASS_REQUIRED", "Для kind=adapter нужен класс адаптера"
        )
    if kind == "mapping":
        if not field_map:
            raise ApiError(400, "FIELD_MAP_REQUIRED", "Для kind=mapping нужен маппинг полей")
        problems = validate_field_map(field_map)
        if problems:
            raise ApiError(400, "FIELD_MAP_INVALID", "; ".join(problems))


@router.get("", response_model=list[SourceResponse], summary="Реестр источников")
async def list_sources(
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> list[SourceResponse]:
    sources = (
        (await session.execute(select(EventSource).order_by(EventSource.code)))
        .scalars()
        .all()
    )
    return [_to_response(source) for source in sources]


@router.post("", response_model=SourceResponse, status_code=201, summary="Регистрация")
async def create_source(
    payload: SourceCreate,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> SourceResponse:
    if await session.get(EventSource, payload.code) is not None:
        raise ApiError(409, "SOURCE_EXISTS", "Источник с таким кодом уже зарегистрирован")
    _check_consistency(payload.kind, payload.adapter_class, payload.field_map)

    source = EventSource(
        code=payload.code,
        name=payload.name,
        kind=payload.kind,
        adapter_class=payload.adapter_class,
        field_map=payload.field_map,
        timezone=payload.timezone,
        ip_allowlist=payload.ip_allowlist,
    )
    session.add(source)
    await session.flush()

    await write_audit(
        session,
        action="source.create",
        entity_type="source",
        entity_id=payload.code,
        actor_id=admin.id,
        new_value={"kind": payload.kind, "adapter_class": payload.adapter_class},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    logger.info("source registered", extra={"source": payload.code, "kind": payload.kind})
    return _to_response(source)


@router.put("/{code}", response_model=SourceResponse, summary="Изменение источника")
async def update_source(
    code: str,
    payload: SourceUpdate,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> SourceResponse:
    source = await _get_source(session, code)
    before = {
        "kind": source.kind,
        "adapter_class": source.adapter_class,
        "field_map": dict(source.field_map or {}),
        "is_active": source.is_active,
    }

    kind = payload.kind or source.kind
    adapter_class = (
        payload.adapter_class if payload.adapter_class is not None else source.adapter_class
    )
    field_map = payload.field_map if payload.field_map is not None else dict(source.field_map)
    _check_consistency(kind, adapter_class, field_map)

    values: dict[str, Any] = {
        "kind": kind,
        "adapter_class": adapter_class,
        "field_map": field_map,
        "updated_at": datetime.now(tz=UTC),
    }
    if payload.name is not None:
        values["name"] = payload.name
    if payload.timezone is not None:
        values["timezone"] = payload.timezone
    if payload.ip_allowlist is not None:
        values["ip_allowlist"] = payload.ip_allowlist
    if payload.is_active is not None:
        values["is_active"] = payload.is_active

    await session.execute(update(EventSource).where(EventSource.code == code).values(**values))
    await session.refresh(source)

    await write_audit(
        session,
        action="source.update",
        entity_type="source",
        entity_id=code,
        actor_id=admin.id,
        old_value=before,
        new_value={
            "kind": source.kind,
            "adapter_class": source.adapter_class,
            "field_map": dict(source.field_map or {}),
            "is_active": source.is_active,
        },
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    return _to_response(source)


@router.delete("/{code}", status_code=204, summary="Отключение источника")
async def deactivate_source(
    code: str,
    request: Request,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Мягкое отключение: принятые события и инциденты остаются на месте.

    Отключение одного источника не влияет на остальные (FR-IN-8).
    """
    source = await _get_source(session, code)
    await session.execute(
        update(EventSource).where(EventSource.code == code).values(is_active=False)
    )
    await write_audit(
        session,
        action="source.deactivate",
        entity_type="source",
        entity_id=code,
        actor_id=admin.id,
        old_value={"is_active": source.is_active},
        new_value={"is_active": False},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()


@router.post("/{code}/rotate-token", response_model=TokenIssued, summary="Ротация токена")
async def rotate_token(
    code: str,
    request: Request,
    with_hmac: bool = False,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> TokenIssued:
    """Ротация токена одного источника не влияет на остальные.

    Значение показывается один раз: в БД хранится только хеш, в журнал аудита
    попадает факт ротации, но не сам токен.
    """
    await _get_source(session, code)

    token = secrets.token_urlsafe(32)
    hmac_secret = secrets.token_bytes(32) if with_hmac else None
    await session.execute(
        update(EventSource)
        .where(EventSource.code == code)
        .values(token_hash=token_hash(token), hmac_secret=hmac_secret)
    )

    await write_audit(
        session,
        action="source.token.rotate",
        entity_type="source",
        entity_id=code,
        actor_id=admin.id,
        new_value={"with_hmac": with_hmac},
        request_id=getattr(request.state, "request_id", None),
    )
    await session.commit()
    logger.info("source token rotated", extra={"source": code})

    return TokenIssued(
        code=code, token=token, hmac_secret=hmac_secret.hex() if hmac_secret else None
    )


@router.post("/{code}/test", response_model=SourceTestResult, summary="Пробный разбор")
async def test_source(
    code: str,
    payload: SourceTestRequest,
    admin: CurrentUser = Depends(require_roles("admin")),
    session: AsyncSession = Depends(get_session),
) -> SourceTestResult:
    """Показывает, во что превратится payload. В БД ничего не пишется."""
    source = await _get_source(session, code)

    field_map = payload.field_map if payload.field_map is not None else dict(source.field_map)
    problems = validate_field_map(field_map) if source.kind == "mapping" else []
    if problems:
        return SourceTestResult(ok=False, error="Маппинг записан неверно", problems=problems)

    # Подменяем маппинг на лету, ничего не сохраняя
    probe = EventSource(
        code=source.code,
        name=source.name,
        kind=source.kind,
        adapter_class=source.adapter_class,
        field_map=field_map,
        timezone=source.timezone,
    )

    try:
        adapter = adapter_for_source(
            probe,
            await load_severity_map(session),
            resolve_timezone(source.timezone, get_settings().zabbix_tz),
        )
        event = adapter.parse(payload.payload)
    except (AdapterError, UnknownAdapterError) as exc:
        return SourceTestResult(ok=False, error=str(exc))

    return SourceTestResult(ok=True, event=event)
