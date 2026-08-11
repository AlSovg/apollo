"""Подстановка локальной модели: OpenAI-совместимый endpoint.

Нужна, пока ВМ с моделью не готова, и для тестов: на ней проверяются таймауты,
отказы и формат ответа. Контракт тот же, что у vLLM/Ollama, поэтому переключение
на настоящую модель — это только смена LLM_BASE_URL и LLM_MODEL.

Управление поведением:
  MOCK_LATENCY_MS — задержка ответа (проверка таймаутов)
  MOCK_STATUS     — код ответа, отличный от 200 (проверка ретраев)
"""

import asyncio
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

LATENCY_MS = int(os.environ.get("MOCK_LATENCY_MS", "0"))
STATUS = int(os.environ.get("MOCK_STATUS", "200"))
MODEL = os.environ.get("MOCK_MODEL", "qwen2.5-7b-instruct")

app = FastAPI(title="Local LLM (mock)")

_requests: list[dict[str, Any]] = []


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    if LATENCY_MS:
        await asyncio.sleep(LATENCY_MS / 1000)
    if STATUS != 200:
        return JSONResponse(
            status_code=STATUS,
            content={"error": {"message": "смоделированная ошибка", "type": "mock"}},
        )

    payload = await request.json()
    _requests.append({"at": time.time(), "payload": payload})

    user_message = next(
        (m["content"] for m in payload.get("messages", []) if m.get("role") == "user"), ""
    )
    # Ответ детерминированный: тесты проверяют конвейер, а не качество модели
    rule = next(
        (line.removeprefix("Правило: ") for line in user_message.splitlines()
         if line.startswith("Правило: ")),
        "инцидент",
    )
    obj = next(
        (line.removeprefix("Объект: ") for line in user_message.splitlines()
         if line.startswith("Объект: ")),
        "объекте",
    )
    content = (
        f"Сработало правило «{rule}» на объекте {obj}. "
        "Это грозит деградацией обслуживания при дальнейшем росте показателя. "
        "Проверьте состояние объекта и свободные ресурсы в первую очередь."
    )

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model", MODEL),
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 128, "completion_tokens": 64, "total_tokens": 192},
    }


@app.get("/v1/models")
async def models() -> Any:
    return {"object": "list", "data": [{"id": MODEL, "object": "model"}]}


@app.get("/mock/requests")
async def requests_log() -> Any:
    """Служебный эндпоинт мока: что уходило в модель."""
    return {"count": len(_requests), "requests": _requests[-5:]}


@app.post("/mock/reset")
async def reset() -> Any:
    _requests.clear()
    return {"status": "ok"}


@app.get("/health")
async def health() -> Any:
    return {"status": "ok"}
