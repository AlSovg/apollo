"""Единый формат ошибок API (раздел 6.8 docs/tz.md).

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

Стектрейсы и детали внутреннего устройства наружу не отдаются.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import InterfaceError, OperationalError

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """Ошибка, которую можно показать клиенту: код и человекочитаемый текст."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers


def error_response(request: Request, error: ApiError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=error.status_code,
        headers=error.headers,
        content={
            "error": {
                "code": error.code,
                "message": error.message,
                "request_id": request_id,
            }
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    async def handle_api_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return error_response(request, exc)

    async def handle_db_unavailable(request: Request, exc: Exception) -> JSONResponse:
        # БД на другом узле: обрыв связи — штатная ситуация, а не сбой кода.
        # 503 + Retry-After позволяет отправителю вебхука повторить запрос;
        # текст исключения не отдаётся — в нём бывает адрес узла БД
        logger.warning("database unavailable: %s", type(exc).__name__)
        return error_response(
            request,
            ApiError(
                503,
                "DATABASE_UNAVAILABLE",
                "База данных временно недоступна, повторите запрос",
                headers={"Retry-After": "5"},
            ),
        )

    app.add_exception_handler(ApiError, handle_api_error)
    # OperationalError/InterfaceError — обрыв соединения под запросом;
    # ConnectionError — отказ на этапе подключения (asyncpg отдаёт его как
    # есть, без обёртки SQLAlchemy); TimeoutError — не уложились в
    # db_connect_timeout_s, когда пакеты до узла БД молча отбрасываются
    for exc_type in (OperationalError, InterfaceError, ConnectionError, TimeoutError):
        app.add_exception_handler(exc_type, handle_db_unavailable)
