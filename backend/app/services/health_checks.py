"""Проверки доступности внешних систем для панели «состояние интеграций».

Проверки живые и с короткими таймаутами: админ должен видеть текущее
положение дел, а не последнюю запись в кеше. Все четыре идут параллельно,
поэтому страница не ждёт их по очереди.

Наружу отдаётся только статус и тип ошибки: ни адресов, ни реквизитов.
"""

import asyncio
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.adapters.identity import LdapIdentityProvider, ldap_settings
from app.config import Settings

logger = logging.getLogger(__name__)

CHECK_TIMEOUT_S = 3.0

OK = "ok"
UNAVAILABLE = "unavailable"
NOT_CONFIGURED = "not_configured"
DISABLED = "disabled"


@dataclass
class IntegrationHealth:
    name: str
    status: str
    detail: str | None = None


async def _tcp_reachable(host: str, port: int) -> bool:
    """Проверка на уровне соединения: подходит и для SMTP, и для TrueConf."""
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_S):
            _, writer = await asyncio.open_connection(host, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def check_directory(settings: Settings) -> IntegrationHealth:
    if settings.auth_provider != "ldap":
        return IntegrationHealth("Каталог (LDAP/AD)", DISABLED, "используется локальный вход")
    if not settings.ldap_host:
        return IntegrationHealth("Каталог (LDAP/AD)", NOT_CONFIGURED)

    provider = LdapIdentityProvider(ldap_settings(settings))
    try:
        connection = await asyncio.to_thread(provider._service_connection)
    except Exception as exc:  # noqa: BLE001 — наружу отдаём только тип
        return IntegrationHealth("Каталог (LDAP/AD)", UNAVAILABLE, type(exc).__name__)
    await asyncio.to_thread(connection.unbind)
    return IntegrationHealth("Каталог (LDAP/AD)", OK)


async def check_llm(settings: Settings) -> IntegrationHealth:
    if not settings.llm_base_url:
        return IntegrationHealth("Языковая модель", NOT_CONFIGURED)
    try:
        async with httpx.AsyncClient(
            base_url=settings.llm_base_url,
            timeout=CHECK_TIMEOUT_S,
            verify=settings.llm_verify_tls,
        ) as client:
            # /v1/models есть у любого OpenAI-совместимого сервера
            response = await client.get("/v1/models")
    except (httpx.TransportError, httpx.TimeoutException) as exc:
        return IntegrationHealth("Языковая модель", UNAVAILABLE, type(exc).__name__)

    if response.status_code >= 500:
        return IntegrationHealth("Языковая модель", UNAVAILABLE, f"HTTP {response.status_code}")
    return IntegrationHealth("Языковая модель", OK, settings.llm_model)


async def check_smtp(settings: Settings) -> IntegrationHealth:
    if not settings.smtp_host:
        return IntegrationHealth("SMTP", NOT_CONFIGURED)
    reachable = await _tcp_reachable(settings.smtp_host, settings.smtp_port)
    return IntegrationHealth("SMTP", OK if reachable else UNAVAILABLE)


async def check_trueconf(settings: Settings) -> IntegrationHealth:
    if not settings.trueconf_base_url:
        return IntegrationHealth("TrueConf", NOT_CONFIGURED)

    parsed = urlparse(settings.trueconf_base_url)
    host = parsed.hostname
    if host is None:
        return IntegrationHealth("TrueConf", NOT_CONFIGURED, "неверный адрес сервера")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    reachable = await _tcp_reachable(host, port)
    return IntegrationHealth("TrueConf", OK if reachable else UNAVAILABLE)


async def check_integrations(settings: Settings) -> list[IntegrationHealth]:
    return list(
        await asyncio.gather(
            check_directory(settings),
            check_llm(settings),
            check_smtp(settings),
            check_trueconf(settings),
        )
    )
