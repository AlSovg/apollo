"""Приём вебхуков: проверка источника и запись сырого события.

Правило 1 из AGENTS.md: обработчик делает ровно один INSERT и отвечает 202.
Payload здесь не разбирается — неожиданное поле не должно приводить к потере
события. Вся интерпретация происходит позже, в нормализаторе.
"""

import hashlib
import hmac
import ipaddress
import json
from typing import Any
from urllib.parse import parse_qs

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.models.events import EventSource, RawEvent

# Заголовки приёма
TOKEN_HEADER = "X-Webhook-Token"
SIGNATURE_HEADER = "X-Webhook-Signature"


async def get_active_source(session: AsyncSession, code: str) -> EventSource:
    source = await session.get(EventSource, code)
    if source is None:
        raise ApiError(404, "SOURCE_UNKNOWN", "Источник не зарегистрирован")
    if not source.is_active:
        raise ApiError(403, "SOURCE_INACTIVE", "Источник отключён")
    return source


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def verify_token(provided: str | None, expected_hash: bytes | None) -> bool:
    """Сравнение постоянного времени: длина совпадения не утекает по таймингу."""
    if expected_hash is None or provided is None:
        return False
    return hmac.compare_digest(token_hash(provided), expected_hash)


def verify_signature(body: bytes, provided: str | None, secret: bytes | None) -> bool:
    if secret is None:
        # Секрет для источника не задан — подпись не требуется
        return True
    if provided is None:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip().lower())


def check_ip_allowed(client_ip: str | None, allowlist: list[str]) -> bool:
    """Пустой список = проверка выключена (по умолчанию)."""
    if not allowlist:
        return True
    if client_ip is None:
        return False
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            network = ipaddress.ip_network(str(entry), strict=False)
        except ValueError:
            continue
        if address in network:
            return True
    return False


def authenticate(
    source: EventSource,
    *,
    body: bytes,
    token: str | None,
    signature: str | None,
    client_ip: str | None,
) -> None:
    """Проверка вебхука. Причина отказа клиенту не раскрывается."""
    if not check_ip_allowed(client_ip, [str(item) for item in source.ip_allowlist]):
        raise ApiError(403, "IP_NOT_ALLOWED", "Адрес источника не разрешён")
    if not verify_token(token, source.token_hash):
        raise ApiError(401, "UNAUTHORIZED", "Неверный токен или подпись источника")
    if not verify_signature(body, signature, source.hmac_secret):
        raise ApiError(401, "UNAUTHORIZED", "Неверный токен или подпись источника")


def _form_to_payload(raw: str) -> dict[str, Any]:
    """Форма и query-строка: одиночные значения разворачиваются из списков."""
    parsed = parse_qs(raw, keep_blank_values=True)
    return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}


def build_payload(body: bytes, content_type: str, query_string: str) -> Any:
    """Тело в JSON, в виде формы или query-параметры (так шлёт SolarWinds)."""
    if body:
        if content_type.split(";")[0].strip() == "application/x-www-form-urlencoded":
            return _form_to_payload(body.decode("utf-8", errors="replace"))
        try:
            return json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # Разбор структуры — это не разбор полей: нераспарсенное тело
            # невозможно положить в jsonb, поэтому отвечаем честной ошибкой
            raise ApiError(400, "INVALID_PAYLOAD", "Тело запроса не является JSON") from exc
    if query_string:
        return _form_to_payload(query_string)
    raise ApiError(400, "EMPTY_PAYLOAD", "Пустой запрос: нет ни тела, ни параметров")


def payload_hash(body: bytes, query_string: str) -> bytes:
    """Хэш того, что реально прислали: тела, а при его отсутствии — query-строки."""
    return hashlib.sha256(body if body else query_string.encode()).digest()


def signed_bytes(body: bytes, query_string: str) -> bytes:
    """То же правило для HMAC: подписывается ровно то, что пришло."""
    return body if body else query_string.encode()


async def store_raw_event(
    session: AsyncSession, *, source: str, payload: Any, digest: bytes
) -> int:
    """Единственная операция приёма. Повторная доставка — не ошибка."""
    statement = (
        insert(RawEvent)
        .values(source=source, payload=payload, payload_hash=digest)
        .on_conflict_do_nothing(constraint="raw_events_dedup_uq")
        .returning(RawEvent.id)
    )
    result = await session.execute(statement)
    raw_event_id = result.scalar_one_or_none()

    if raw_event_id is None:
        # Тот же payload в ту же микросекунду — отдаём id уже принятого события
        existing = await session.execute(
            select(RawEvent.id)
            .where(RawEvent.source == source, RawEvent.payload_hash == digest)
            .order_by(RawEvent.received_at.desc())
            .limit(1)
        )
        raw_event_id = existing.scalar_one()

    await session.commit()
    return int(raw_event_id)
