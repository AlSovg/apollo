"""Мок-сервер TrueConf Server.

Доступа к боевому серверу мессенджера у команды нет, поэтому канал проверяется
против подстановки, повторяющей контракт: те же пути, коды ответов, формат
ошибок, искусственные задержки и отказы. Заглушка, которая пишет в лог и
возвращает успех, требованию кейса не отвечает — на такой не проверишь ни
ретраи, ни обработку 401.

Управление поведением (для тестов и демонстрации отказов):
  MOCK_LATENCY_MS   — искусственная задержка ответа
  MOCK_FAIL_RATE    — доля запросов, отвечающих 500
Получатель с идентификатором, начинающимся на 'fail-', всегда даёт 500,
на 'unknown-' — 404: так отказ воспроизводится детерминированно.
"""

import asyncio
import os
import random
import time
import uuid
from typing import Any

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse

CLIENT_ID = os.environ.get("MOCK_CLIENT_ID", "apollo")
CLIENT_SECRET = os.environ.get("MOCK_CLIENT_SECRET", "apollo-secret")
LATENCY_MS = int(os.environ.get("MOCK_LATENCY_MS", "0"))
FAIL_RATE = float(os.environ.get("MOCK_FAIL_RATE", "0"))
TOKEN_TTL_S = int(os.environ.get("MOCK_TOKEN_TTL_S", "3600"))

app = FastAPI(title="TrueConf Server (mock)")

_tokens: dict[str, float] = {}
_delivered: list[dict[str, Any]] = []


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.post("/oauth/v2/token")
async def issue_token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
) -> Any:
    if grant_type != "client_credentials":
        return _error(400, "unsupported_grant_type", "Поддерживается только client_credentials")
    if client_id != CLIENT_ID or client_secret != CLIENT_SECRET:
        return _error(401, "invalid_client", "Неверные реквизиты приложения")

    token = uuid.uuid4().hex
    _tokens[token] = time.time() + TOKEN_TTL_S
    return {"access_token": token, "token_type": "Bearer", "expires_in": TOKEN_TTL_S}


@app.post("/api/v3.2/chats/{user_id}/messages")
async def send_message(
    user_id: str, request: Request, authorization: str = Header(default="")
) -> Any:
    if LATENCY_MS:
        await asyncio.sleep(LATENCY_MS / 1000)

    token = authorization.removeprefix("Bearer ").strip()
    if not token or _tokens.get(token, 0) < time.time():
        return _error(401, "invalid_token", "Токен недействителен или истёк")

    if user_id.startswith("unknown-"):
        return _error(404, "user_not_found", "Пользователь не найден")
    if user_id.startswith("fail-") or (FAIL_RATE and random.random() < FAIL_RATE):
        return _error(500, "internal_error", "Внутренняя ошибка сервера")

    body = await request.json()
    _delivered.append({"user_id": user_id, "body": body, "at": time.time()})
    return JSONResponse(status_code=200, content={"message_id": uuid.uuid4().hex})


@app.get("/mock/delivered")
async def delivered() -> Any:
    """Служебный эндпоинт мока: что дошло. У боевого сервера его нет."""
    return {"count": len(_delivered), "messages": _delivered[-20:]}


@app.post("/mock/reset")
async def reset() -> Any:
    _delivered.clear()
    return {"status": "ok"}


@app.get("/health")
async def health() -> Any:
    return {"status": "ok"}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return _error(exc.status_code, "error", str(exc.detail))
