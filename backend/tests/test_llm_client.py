"""Клиент локальной модели против OpenAI-совместимого мока."""

import os

import httpx
import pytest

from app.adapters.llm import LlmSettings, LlmUnavailableError, OpenAiCompatibleClient
from app.adapters.llm.openai_compatible import _extract_content

BASE_URL = os.environ.get("TEST_LLM_URL", "http://127.0.0.1:18081")


def settings(**overrides: object) -> LlmSettings:
    defaults: dict[str, object] = {
        "base_url": BASE_URL,
        "model": "qwen2.5-7b-instruct",
        "timeout_s": 10.0,
        "max_attempts": 2,
        "verify_tls": False,
    }
    defaults.update(overrides)
    return LlmSettings(**defaults)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def llm_available() -> None:
    try:
        httpx.get(f"{BASE_URL}/health", timeout=3).raise_for_status()
    except Exception as exc:  # noqa: BLE001 — причина видна в пропуске
        pytest.skip(f"Мок модели недоступен ({type(exc).__name__}: {exc})")


async def test_summary_is_returned(llm_available: None) -> None:
    client = OpenAiCompatibleClient(settings())

    result = await client.summarize(
        "Ты помощник дежурного инженера.",
        "Правило: Диск заполнен на 94%\nОбъект: db-prod-03",
    )

    assert "db-prod-03" in result.content
    assert result.model == "qwen2.5-7b-instruct"
    assert result.latency_ms >= 0


async def test_prompt_and_context_are_sent_separately(llm_available: None) -> None:
    client = OpenAiCompatibleClient(settings())
    httpx.post(f"{BASE_URL}/mock/reset", timeout=5)

    await client.summarize("СИСТЕМНЫЙ ПРОМПТ", "КОНТЕКСТ ИНЦИДЕНТА")

    sent = httpx.get(f"{BASE_URL}/mock/requests", timeout=5).json()["requests"][-1]
    messages = sent["payload"]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "СИСТЕМНЫЙ ПРОМПТ"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "КОНТЕКСТ ИНЦИДЕНТА"
    # Низкая температура: сводка должна быть воспроизводимой
    assert sent["payload"]["temperature"] <= 0.3


async def test_unavailable_model_raises_dedicated_error() -> None:
    """Недоступность модели отличима от любой другой ошибки."""
    client = OpenAiCompatibleClient(settings(base_url="http://127.0.0.1:1", timeout_s=1.0))

    with pytest.raises(LlmUnavailableError):
        await client.summarize("промпт", "контекст")


async def test_timeout_is_reported_as_unavailable() -> None:
    """Молчащая модель — это тоже недоступность, а не ошибка приложения.

    Адрес неройтящийся: пакеты уходят в никуда, поэтому срабатывает именно
    таймаут. Полагаться на «медленный ответ» локального мока нельзя — на
    localhost миллисекунды плавают, и тест становится шатким.
    """
    client = OpenAiCompatibleClient(
        settings(base_url="http://10.255.255.1:8080", timeout_s=0.5, max_attempts=1)
    )

    with pytest.raises(LlmUnavailableError):
        await client.summarize("промпт", "контекст")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": "   "}}]},
        {"choices": [{"message": {"content": 42}}]},
    ],
)
def test_broken_answers_are_rejected(payload: dict[str, object]) -> None:
    """Пустой ответ не должен превращаться в пустую сводку в карточке."""
    assert _extract_content(payload) is None


def test_valid_answer_is_extracted_and_trimmed() -> None:
    payload = {"choices": [{"message": {"content": "  Сводка инцидента.  "}}]}

    assert _extract_content(payload) == "Сводка инцидента."


async def test_thinking_is_disabled_in_request(llm_available: None) -> None:
    """Qwen3 без этого флага тратит секунды и токены на блок <think>."""
    client = OpenAiCompatibleClient(settings())
    httpx.post(f"{BASE_URL}/mock/reset", timeout=5)

    await client.summarize("промпт", "контекст")

    sent = httpx.get(f"{BASE_URL}/mock/requests", timeout=5).json()["requests"][-1]
    assert sent["payload"]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_thinking_flag_can_be_turned_off(llm_available: None) -> None:
    """Серверы без поддержки этого поля не должны получать лишний ключ."""
    client = OpenAiCompatibleClient(settings(disable_thinking=False))
    httpx.post(f"{BASE_URL}/mock/reset", timeout=5)

    await client.summarize("промпт", "контекст")

    sent = httpx.get(f"{BASE_URL}/mock/requests", timeout=5).json()["requests"][-1]
    assert "chat_template_kwargs" not in sent["payload"]


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("<think>Прикидываю…</think>\nСводка инцидента.", "Сводка инцидента."),
        ("<think>a</think><think>b</think> Итог.", "Итог."),
        ("Сводка без рассуждений.", "Сводка без рассуждений."),
    ],
)
def test_reasoning_block_is_cut_off(answer: str, expected: str) -> None:
    """Внутренний монолог модели не должен попадать в карточку инцидента."""
    assert _extract_content({"choices": [{"message": {"content": answer}}]}) == expected


def test_unfinished_reasoning_is_not_a_summary() -> None:
    """Ответ, упёршийся в max_tokens внутри рассуждений, полезного текста не несёт."""
    payload = {"choices": [{"message": {"content": "<think>Смотрю на данные и"}}]}

    assert _extract_content(payload) is None
