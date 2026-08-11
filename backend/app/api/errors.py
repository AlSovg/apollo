"""Единый формат ошибок API (раздел 6.8 docs/tz.md).

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

Стектрейсы и детали внутреннего устройства наружу не отдаются.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


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

    app.add_exception_handler(ApiError, handle_api_error)
