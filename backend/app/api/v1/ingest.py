"""Эндпоинты приёма вебхуков.

Ровно один INSERT и ответ 202 (FR-IN-2). Никакого разбора полей: система,
приславшая незнакомое поле, не должна терять событие.
"""

import logging
from functools import lru_cache

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.config import get_settings
from app.db import get_session
from app.schemas.ingest import IngestAccepted
from app.services.ingest import (
    SIGNATURE_HEADER,
    TOKEN_HEADER,
    authenticate,
    build_payload,
    get_active_source,
    payload_hash,
    signed_bytes,
    store_raw_event,
)
from app.services.rate_limit import RateLimiter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/ingest", tags=["ingest"])

@lru_cache
def get_limiter() -> RateLimiter:
    """Ведро на источник: шторм одного источника не глушит остальные."""
    return RateLimiter(rate=float(get_settings().ingest_rate_limit_rps))


def client_ip(request: Request) -> str | None:
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("X-Real-IP") or request.headers.get(
            "X-Forwarded-For"
        )
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None


async def read_body(request: Request) -> bytes:
    """Тело сверх лимита не читается целиком в память."""
    limit = get_settings().ingest_max_body_bytes

    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "PAYLOAD_TOO_LARGE", "Тело запроса превышает допустимый размер")

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(
                413, "PAYLOAD_TOO_LARGE", "Тело запроса превышает допустимый размер"
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(
    "/{source_code}",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestAccepted,
    summary="Приём события от системы мониторинга",
)
async def ingest(
    source_code: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> IngestAccepted:
    if not get_limiter().allow(source_code):
        raise ApiError(
            429,
            "RATE_LIMITED",
            "Превышена допустимая частота приёма",
            headers={"Retry-After": "1"},
        )

    source = await get_active_source(session, source_code)
    body = await read_body(request)
    query_string = request.url.query

    authenticate(
        source,
        body=signed_bytes(body, query_string),
        token=request.headers.get(TOKEN_HEADER),
        signature=request.headers.get(SIGNATURE_HEADER),
        client_ip=client_ip(request),
    )

    payload = build_payload(
        body, request.headers.get("content-type", ""), query_string
    )
    raw_event_id = await store_raw_event(
        session,
        source=source.code,
        payload=payload,
        digest=payload_hash(body, query_string),
    )

    # В лог — только факт и идентификаторы: payload события не логируется
    logger.info(
        "event accepted", extra={"source": source.code, "raw_event_id": raw_event_id}
    )
    return IngestAccepted(status="accepted", raw_event_id=raw_event_id)
