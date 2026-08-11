"""Сессии: выпуск, проверка и ротация токенов.

Токены живут в httpOnly Secure SameSite=Strict cookie и в теле ответа не
возвращаются — так их не достанет XSS. Access короткий (15 минут), refresh
ротируется при каждом обновлении: перехваченный старый токен становится
бесполезен, а его повторное предъявление видно как использование отозванного.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ApiError
from app.models.identity import Session as SessionRow
from app.models.identity import User

logger = logging.getLogger(__name__)

ALGORITHM = "HS256"
ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
COOKIE_PATH = "/api"

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_REFRESH = "refresh"


@dataclass
class TokenPair:
    access: str
    refresh: str
    refresh_jti: str
    refresh_expires_at: datetime


class InvalidTokenError(ApiError):
    def __init__(self, message: str = "Сессия недействительна") -> None:
        super().__init__(401, "INVALID_TOKEN", message)


def encode_token(
    *, secret: str, subject_id: str, token_type: str, ttl: timedelta, jti: str | None = None
) -> tuple[str, str, datetime]:
    now = datetime.now(tz=UTC)
    expires_at = now + ttl
    token_id = jti or uuid.uuid4().hex
    payload = {
        "sub": subject_id,
        "typ": token_type,
        "jti": token_id,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM), token_id, expires_at


def decode_token(token: str, *, secret: str, expected_type: str) -> dict[str, str]:
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError as exc:
        # Причина наружу не раскрывается: истёк, подделан — для клиента одно и то же
        raise InvalidTokenError() from exc
    if payload.get("typ") != expected_type:
        raise InvalidTokenError()
    return payload


async def issue_session(
    session: AsyncSession,
    user: User,
    *,
    secret: str,
    access_ttl: timedelta,
    refresh_ttl: timedelta,
) -> TokenPair:
    access, _, _ = encode_token(
        secret=secret,
        subject_id=user.subject_id,
        token_type=TOKEN_TYPE_ACCESS,
        ttl=access_ttl,
    )
    refresh, jti, expires_at = encode_token(
        secret=secret,
        subject_id=user.subject_id,
        token_type=TOKEN_TYPE_REFRESH,
        ttl=refresh_ttl,
    )
    session.add(SessionRow(user_id=user.id, jti=jti, expires_at=expires_at))
    await session.flush()
    return TokenPair(
        access=access, refresh=refresh, refresh_jti=jti, refresh_expires_at=expires_at
    )


async def active_session(session: AsyncSession, jti: str) -> SessionRow:
    row = (
        await session.execute(select(SessionRow).where(SessionRow.jti == jti))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        # Предъявление отозванного токена — сигнал, который стоит видеть
        logger.warning("auth: предъявлен отозванный или неизвестный refresh-токен")
        raise InvalidTokenError()
    if row.expires_at <= datetime.now(tz=UTC):
        raise InvalidTokenError()
    return row


async def rotate_session(
    session: AsyncSession,
    user: User,
    previous: SessionRow,
    *,
    secret: str,
    access_ttl: timedelta,
    refresh_ttl: timedelta,
) -> TokenPair:
    tokens = await issue_session(
        session, user, secret=secret, access_ttl=access_ttl, refresh_ttl=refresh_ttl
    )
    await session.execute(
        update(SessionRow)
        .where(SessionRow.id == previous.id)
        .values(revoked_at=datetime.now(tz=UTC), replaced_by=tokens.refresh_jti)
    )
    return tokens


async def revoke_session(session: AsyncSession, jti: str) -> None:
    await session.execute(
        update(SessionRow)
        .where(SessionRow.jti == jti, SessionRow.revoked_at.is_(None))
        .values(revoked_at=datetime.now(tz=UTC))
    )


def set_auth_cookies(
    response: Response, tokens: TokenPair, *, secure: bool, access_ttl: timedelta,
    refresh_ttl: timedelta,
) -> None:
    for name, value, ttl in (
        (ACCESS_COOKIE, tokens.access, access_ttl),
        (REFRESH_COOKIE, tokens.refresh, refresh_ttl),
    ):
        response.set_cookie(
            key=name,
            value=value,
            max_age=int(ttl.total_seconds()),
            httponly=True,
            secure=secure,
            samesite="strict",
            path=COOKIE_PATH,
        )


def clear_auth_cookies(response: Response) -> None:
    for name in (ACCESS_COOKIE, REFRESH_COOKIE):
        response.delete_cookie(key=name, path=COOKIE_PATH)
