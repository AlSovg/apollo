"""Мок TrueConf Server (Chatbot Connector).

Контракт снят с боевого сервера 5.5: JWT учётки бота по
/bridge/api/client/v1/oauth/token (grant password, ответ 201) и WebSocket
/websocket/chat_bot/ с методами auth / createP2PChat / sendMessage. Формат
ошибок — payload.errorCode, как у боевого (201 — авторизация, 304 — доступ).

Заглушка, которая пишет в лог и возвращает успех, требованию кейса не
отвечает — на такой не проверишь ни обработку протухшего токена, ни отказов.

Управление поведением (для тестов и демонстрации отказов):
  MOCK_LATENCY_MS   — искусственная задержка ответов
  MOCK_FAIL_RATE    — доля сообщений, завершающихся ошибкой
Получатель, начинающийся на 'fail-', детерминированно даёт errorCode при
отправке; боевой сервер несуществующих получателей не отвергает — мок
воспроизводит и это: чат создаётся для любого userId.
"""

import asyncio
import os
import random
import time
import uuid
from hashlib import sha1
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

USERNAME = os.environ.get("MOCK_USERNAME", "apollo-bot")
PASSWORD = os.environ.get("MOCK_PASSWORD", "apollo-bot-pass")
LATENCY_MS = int(os.environ.get("MOCK_LATENCY_MS", "0"))
FAIL_RATE = float(os.environ.get("MOCK_FAIL_RATE", "0"))
TOKEN_TTL_S = int(os.environ.get("MOCK_TOKEN_TTL_S", "3600"))

# Коды ошибок боевого сервера
ERROR_AUTH = 201
ERROR_FORBIDDEN = 304

app = FastAPI(title="TrueConf Server (mock, Chatbot Connector)")

_tokens: dict[str, float] = {}
_chats: dict[str, str] = {}  # chatId -> userId получателя
_delivered: list[dict[str, Any]] = []


@app.post("/bridge/api/client/v1/oauth/token")
async def issue_token(request: Request) -> Any:
    if LATENCY_MS:
        await asyncio.sleep(LATENCY_MS / 1000)

    body = await request.json()
    if body.get("grant_type") != "password" or body.get("client_id") != "chat_bot":
        return JSONResponse(
            status_code=401,
            content={"error": "invalid_grant", "reason": "invalid grant_type"},
        )
    if body.get("username") != USERNAME or body.get("password") != PASSWORD:
        return JSONResponse(
            status_code=401,
            content={
                "error": "invalid_grant",
                "error_description": "Invalid username or password!",
            },
        )

    token = uuid.uuid4().hex
    expires_at = int(time.time()) + TOKEN_TTL_S
    _tokens[token] = expires_at
    # Боевой сервер отвечает 201 и unix-временем истечения
    return JSONResponse(
        status_code=201,
        content={"access_token": token, "token_type": "JWT", "expires_at": expires_at},
    )


def _answer(request_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": 2, "id": request_id, "payload": payload}


@app.websocket("/websocket/chat_bot/")
async def chat_bot(ws: WebSocket) -> None:
    await ws.accept()
    authorized = False
    try:
        while True:
            message = await ws.receive_json()
            request_id = message.get("id", 0)
            method = message.get("method")
            payload = message.get("payload") or {}

            if LATENCY_MS:
                await asyncio.sleep(LATENCY_MS / 1000)

            if method == "auth":
                token = payload.get("token", "")
                if _tokens.get(token, 0) < time.time():
                    await ws.send_json(_answer(request_id, {"errorCode": ERROR_AUTH}))
                    continue
                authorized = True
                await ws.send_json(
                    _answer(
                        request_id,
                        {
                            "userId": f"{USERNAME}@mock/{uuid.uuid4().hex[:8]}",
                            "connectionId": str(uuid.uuid4()),
                        },
                    )
                )
                continue

            if not authorized:
                await ws.send_json(_answer(request_id, {"errorCode": ERROR_AUTH}))
                continue

            if method == "createP2PChat":
                user_id = str(payload.get("userId", ""))
                # Как боевой сервер: существование получателя не проверяется
                chat_id = sha1(user_id.encode()).hexdigest()
                _chats[chat_id] = user_id
                await ws.send_json(_answer(request_id, {"chatId": chat_id}))
                continue

            if method == "sendMessage":
                chat_id = str(payload.get("chatId", ""))
                user_id = _chats.get(chat_id)
                if user_id is None:
                    await ws.send_json(
                        _answer(request_id, {"errorCode": ERROR_FORBIDDEN})
                    )
                    continue
                if user_id.startswith("fail-") or (
                    FAIL_RATE and random.random() < FAIL_RATE
                ):
                    await ws.send_json(
                        _answer(request_id, {"errorCode": ERROR_FORBIDDEN})
                    )
                    continue
                _delivered.append(
                    {
                        "user_id": user_id,
                        "chat_id": chat_id,
                        "content": payload.get("content"),
                        "at": time.time(),
                    }
                )
                await ws.send_json(
                    _answer(
                        request_id,
                        {
                            "chatId": chat_id,
                            "messageId": str(uuid.uuid4()),
                            "timestamp": int(time.time() * 1000),
                        },
                    )
                )
                continue

            await ws.send_json(_answer(request_id, {"errorCode": ERROR_FORBIDDEN}))
    except WebSocketDisconnect:
        pass


@app.get("/mock/delivered")
async def delivered() -> Any:
    """Служебный эндпоинт мока: что дошло. У боевого сервера его нет."""
    return {"count": len(_delivered), "messages": _delivered[-20:]}


@app.post("/mock/reset")
async def reset() -> Any:
    _delivered.clear()
    _chats.clear()
    return {"status": "ok"}


@app.get("/health")
async def health() -> Any:
    return {"status": "ok"}
