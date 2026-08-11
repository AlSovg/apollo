"""Вход, обновление сессии, выход и сведения о текущем пользователе."""

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity import (
    DirectoryUnavailableError,
    LdapIdentityProvider,
    ldap_settings,
)
from app.api.deps import CurrentUser, current_user
from app.config import Settings, get_settings
from app.db import get_session
from app.models.identity import User
from app.schemas.auth import CurrentUserResponse, LoginRequest, SessionStatus
from app.services.audit import write_audit
from app.services.auth import (
    REFRESH_COOKIE,
    TOKEN_TYPE_REFRESH,
    InvalidTokenError,
    active_session,
    clear_auth_cookies,
    decode_token,
    issue_session,
    revoke_session,
    rotate_session,
    set_auth_cookies,
)
from app.services.login import login as login_flow

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _ttls(settings: Settings) -> tuple[timedelta, timedelta]:
    return (
        timedelta(minutes=settings.access_token_ttl_minutes),
        timedelta(hours=settings.refresh_token_ttl_hours),
    )


@router.post("/login", response_model=SessionStatus, summary="Вход")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> SessionStatus:
    settings = get_settings()
    request_id = getattr(request.state, "request_id", None)

    user, _ = await login_flow(
        session,
        username=payload.username,
        password=payload.password,
        settings=settings,
        request_id=request_id,
    )

    access_ttl, refresh_ttl = _ttls(settings)
    tokens = await issue_session(
        session,
        user,
        secret=settings.jwt_secret,
        access_ttl=access_ttl,
        refresh_ttl=refresh_ttl,
    )
    await session.commit()

    set_auth_cookies(
        response,
        tokens,
        secure=settings.cookie_secure,
        access_ttl=access_ttl,
        refresh_ttl=refresh_ttl,
    )
    # В лог — только факт входа: ни пароля, ни токена
    logger.info("login succeeded", extra={"user_id": user.id})
    return SessionStatus(status="ok")


@router.post("/refresh", response_model=SessionStatus, summary="Обновление сессии")
async def refresh(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> SessionStatus:
    settings = get_settings()
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise InvalidTokenError("Требуется аутентификация")

    payload = decode_token(
        token, secret=settings.jwt_secret, expected_type=TOKEN_TYPE_REFRESH
    )
    row = await active_session(session, payload["jti"])

    user = (
        await session.execute(select(User).where(User.id == row.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise InvalidTokenError("Учётная запись недоступна")

    access_ttl, refresh_ttl = _ttls(settings)
    tokens = await rotate_session(
        session,
        user,
        row,
        secret=settings.jwt_secret,
        access_ttl=access_ttl,
        refresh_ttl=refresh_ttl,
    )
    await session.commit()

    set_auth_cookies(
        response,
        tokens,
        secure=settings.cookie_secure,
        access_ttl=access_ttl,
        refresh_ttl=refresh_ttl,
    )
    return SessionStatus(status="ok")


@router.post("/logout", response_model=SessionStatus, summary="Выход")
async def logout(
    request: Request, response: Response, session: AsyncSession = Depends(get_session)
) -> SessionStatus:
    settings = get_settings()
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        try:
            payload = decode_token(
                token, secret=settings.jwt_secret, expected_type=TOKEN_TYPE_REFRESH
            )
        except InvalidTokenError:
            payload = {}
        if payload.get("jti"):
            await revoke_session(session, payload["jti"])
            await write_audit(
                session,
                action="user.logout",
                entity_type="user",
                request_id=getattr(request.state, "request_id", None),
            )
            await session.commit()

    clear_auth_cookies(response)
    return SessionStatus(status="ok")


@router.get("/me", response_model=CurrentUserResponse, summary="Текущий пользователь")
async def me(user: CurrentUser = Depends(current_user)) -> CurrentUserResponse:
    settings = get_settings()
    display_name: str | None = None

    if settings.auth_provider == "ldap" and user.user.provider == "ldap":
        # Имя подтягивается из каталога на лету и в БД не сохраняется (ПДн)
        try:
            subject = await LdapIdentityProvider(ldap_settings(settings)).describe(
                user.subject_id
            )
            display_name = subject.display_name if subject else None
        except DirectoryUnavailableError:
            # Недоступность каталога не должна ломать интерфейс
            display_name = None

    return CurrentUserResponse(
        subject_id=user.subject_id,
        username=user.user.username,
        roles=user.roles,
        display_name=display_name,
    )
