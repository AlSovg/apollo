"""Клиент локальной модели по OpenAI-совместимому API.

Работает с любым сервером, отдающим /v1/chat/completions — vLLM, Ollama,
llama.cpp, TGI. Смена модели или сервера — это переменные окружения,
код не меняется.

Модель развёрнута на отдельной машине и общается с ядром по HTTP: её
недоступность или перегрузка не влияют на сквозной путь доставки (FR-AI-7).
"""

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from app.adapters.llm.base import LlmResult, LlmUnavailableError

logger = logging.getLogger(__name__)

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"

REASONING_START = "<think>"
REASONING_END = "</think>"


@dataclass
class LlmSettings:
    base_url: str
    model: str
    api_key: str = ""
    timeout_s: float = 30.0
    max_attempts: int = 2
    temperature: float = 0.2
    max_tokens: int = 400
    verify_tls: bool = True
    # Qwen3 — гибридная reasoning-модель: без этого флага она сначала пишет
    # рассуждения в блоке <think>, тратя на них токены и секунды
    disable_thinking: bool = True


class OpenAiCompatibleClient:
    name = "openai-compatible"

    def __init__(self, settings: LlmSettings) -> None:
        self._settings = settings

    def _headers(self) -> dict[str, str]:
        # Локальные серверы обычно не требуют ключа, но vLLM умеет его проверять
        if self._settings.api_key:
            return {"Authorization": f"Bearer {self._settings.api_key}"}
        return {}

    async def summarize(self, prompt: str, context: str) -> LlmResult:
        settings = self._settings
        payload: dict[str, object] = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": context},
            ],
            # Низкая температура: сводка должна быть воспроизводимой
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
            "stream": False,
        }
        if settings.disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        last_error: Exception | None = None
        for attempt in range(1, settings.max_attempts + 1):
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(
                    base_url=settings.base_url,
                    timeout=settings.timeout_s,
                    verify=settings.verify_tls,
                ) as client:
                    response = await client.post(
                        CHAT_COMPLETIONS_PATH, json=payload, headers=self._headers()
                    )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning(
                    "llm: обращение не удалось",
                    extra={"attempt": attempt, "reason": type(exc).__name__},
                )
                await asyncio.sleep(0)
                continue

            if response.status_code != 200:
                last_error = LlmUnavailableError(f"модель ответила {response.status_code}")
                logger.warning(
                    "llm: неуспешный ответ",
                    extra={"attempt": attempt, "status": response.status_code},
                )
                continue

            content = _extract_content(response.json())
            if content is None:
                last_error = LlmUnavailableError("модель вернула пустой ответ")
                continue

            return LlmResult(
                content=content,
                model=settings.model,
                latency_ms=int((time.perf_counter() - started) * 1000),
            )

        raise LlmUnavailableError(
            f"модель недоступна после {settings.max_attempts} попыток: "
            f"{type(last_error).__name__ if last_error else 'неизвестно'}"
        )


def _extract_content(payload: Mapping[str, object]) -> str | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str):
        return None
    stripped = strip_reasoning(content)
    return stripped or None


def strip_reasoning(content: str) -> str:
    """Убирает блок рассуждений из ответа.

    Основной способ их отключить — chat_template_kwargs, но шаблон модели
    может его не поддержать (другой сервер, другая сборка). Тогда в сводку
    попал бы внутренний монолог модели, а дежурному нужен результат.

    Незакрытый блок означает, что ответ упёрся в max_tokens прямо в
    рассуждениях: полезного текста нет, пустая строка уводит вызов в повтор.
    """
    if REASONING_END in content:
        return content.rsplit(REASONING_END, 1)[-1].strip()
    if REASONING_START in content:
        return ""
    return content.strip()
