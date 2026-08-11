"""Дублирующий канал — электронная почта.

smtplib синхронный, поэтому отправка уходит в поток: воркер доставки не должен
блокироваться на медленном сервере. TLS обязателен — уведомления ходят
внутри контура, но пароли и адреса по открытому каналу не гоняются.
"""

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.adapters.channels.base import (
    NotificationMessage,
    PermanentChannelError,
    TransientChannelError,
)

logger = logging.getLogger(__name__)


@dataclass
class SmtpSettings:
    host: str
    port: int
    username: str
    password: str
    sender: str
    use_tls: bool = True
    timeout_s: int = 15


class EmailChannel:
    name = "email"

    def __init__(self, settings: SmtpSettings) -> None:
        self._settings = settings

    def _build(self, message: NotificationMessage, address: str) -> EmailMessage:
        email = EmailMessage()
        email["From"] = self._settings.sender
        email["To"] = address
        email["Subject"] = message.subject
        email.set_content(message.body())
        return email

    def _send(self, email: EmailMessage) -> None:
        settings = self._settings
        with smtplib.SMTP(settings.host, settings.port, timeout=settings.timeout_s) as client:
            if settings.use_tls:
                client.starttls(context=ssl.create_default_context())
            if settings.username:
                client.login(settings.username, settings.password)
            client.send_message(email)

    async def send(self, message: NotificationMessage, address: str | None) -> None:
        if not address:
            raise PermanentChannelError("не удалось определить адрес получателя")

        try:
            await asyncio.to_thread(self._send, self._build(message, address))
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPNotSupportedError) as exc:
            raise PermanentChannelError(f"SMTP отверг сообщение: {type(exc).__name__}") from exc
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            # Текст ошибки может содержать адрес получателя — наружу только тип
            raise TransientChannelError(f"SMTP недоступен: {type(exc).__name__}") from exc
