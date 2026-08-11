"""Канал TrueConf против мок-сервера.

Мок повторяет контракт серверного API: OAuth-токен приложения, отправка
сообщения, коды 401/404/500 и формат ошибок. Проверять канал на заглушке,
которая всегда возвращает успех, бессмысленно — не увидишь ни ретраев,
ни обработки протухшего токена.
"""

import os

import httpx
import pytest

from app.adapters.channels import (
    NotificationMessage,
    PermanentChannelError,
    TransientChannelError,
    TrueConfChannel,
    TrueConfSettings,
)
from tests.factories import make_incident  # noqa: F401  (используется косвенно)

BASE_URL = os.environ.get("TEST_TRUECONF_URL", "http://127.0.0.1:18080")


def settings(**overrides: object) -> TrueConfSettings:
    defaults: dict[str, object] = {
        "base_url": BASE_URL,
        "client_id": "apollo",
        "client_secret": "apollo-secret",
        "timeout_s": 5.0,
        "verify_tls": False,
    }
    defaults.update(overrides)
    return TrueConfSettings(**defaults)  # type: ignore[arg-type]


def message(notification_id: int = 1) -> NotificationMessage:
    from datetime import UTC, datetime

    return NotificationMessage(
        notification_id=notification_id,
        incident_id=42,
        severity=4,
        rule_name="Диск заполнен на 94%",
        object_name="db-prod-03",
        opened_at=datetime(2026, 8, 10, 10, 53, 52, tzinfo=UTC),
        message="Диск заполнен на 94%",
    )


@pytest.fixture(scope="session")
def trueconf_available() -> None:
    try:
        response = httpx.get(f"{BASE_URL}/health", timeout=3)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — причина видна в пропуске
        pytest.skip(f"Мок TrueConf недоступен ({type(exc).__name__}: {exc})")


async def test_message_is_delivered(trueconf_available: None) -> None:
    channel = TrueConfChannel(settings())
    httpx.post(f"{BASE_URL}/mock/reset", timeout=5)

    await channel.send(message(), "user-42")

    delivered = httpx.get(f"{BASE_URL}/mock/delivered", timeout=5).json()
    assert delivered["count"] == 1
    assert delivered["messages"][0]["user_id"] == "user-42"
    # Текст ушёл целиком: дежурный видит и правило, и объект
    assert "db-prod-03" in delivered["messages"][0]["body"]["content"]["text"]


async def test_server_error_is_transient(trueconf_available: None) -> None:
    """500 — повод повторить, а не признать доставку проваленной."""
    channel = TrueConfChannel(settings())

    with pytest.raises(TransientChannelError):
        await channel.send(message(), "fail-500")


async def test_unknown_recipient_is_permanent(trueconf_available: None) -> None:
    """404 повторять бессмысленно: это проблема данных, а не связи."""
    channel = TrueConfChannel(settings())

    with pytest.raises(PermanentChannelError):
        await channel.send(message(), "unknown-user")


async def test_missing_address_is_permanent(trueconf_available: None) -> None:
    channel = TrueConfChannel(settings())

    with pytest.raises(PermanentChannelError):
        await channel.send(message(), None)


async def test_wrong_credentials_are_rejected(trueconf_available: None) -> None:
    channel = TrueConfChannel(settings(client_secret="не тот секрет"))

    with pytest.raises(PermanentChannelError):
        await channel.send(message(), "user-42")


async def test_unreachable_server_is_transient() -> None:
    """Недоступность сервера отличается от отказа: уведомление уйдёт в ретрай."""
    channel = TrueConfChannel(settings(base_url="http://127.0.0.1:1", timeout_s=1.0))

    with pytest.raises(TransientChannelError):
        await channel.send(message(), "user-42")


async def test_token_is_reused_between_sends(trueconf_available: None) -> None:
    channel = TrueConfChannel(settings())
    httpx.post(f"{BASE_URL}/mock/reset", timeout=5)

    await channel.send(message(1), "user-1")
    first_token = channel._token
    await channel.send(message(2), "user-2")

    assert channel._token == first_token  # повторная авторизация не понадобилась
    assert httpx.get(f"{BASE_URL}/mock/delivered", timeout=5).json()["count"] == 2
