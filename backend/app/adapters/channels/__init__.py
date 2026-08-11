"""Реестр каналов доставки.

Набор каналов расширяется новым классом без правки маршрутизатора (FR-DLV-3).
"""

from app.adapters.channels.base import (
    ChannelError,
    NotificationChannel,
    NotificationMessage,
    PermanentChannelError,
    TransientChannelError,
)
from app.adapters.channels.email import EmailChannel, SmtpSettings
from app.adapters.channels.trueconf import TrueConfChannel, TrueConfSettings
from app.adapters.channels.web import WebChannel
from app.config import Settings


def build_channels(settings: Settings) -> dict[str, NotificationChannel]:
    return {
        "web": WebChannel(),
        "email": EmailChannel(
            SmtpSettings(
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=settings.smtp_password,
                sender=settings.smtp_sender,
                use_tls=settings.smtp_use_tls,
            )
        ),
        "trueconf": TrueConfChannel(
            TrueConfSettings(
                base_url=settings.trueconf_base_url,
                username=settings.trueconf_username,
                password=settings.trueconf_password,
                verify_tls=settings.trueconf_verify_tls,
            )
        ),
    }


__all__ = [
    "ChannelError",
    "EmailChannel",
    "NotificationChannel",
    "NotificationMessage",
    "PermanentChannelError",
    "SmtpSettings",
    "TransientChannelError",
    "TrueConfChannel",
    "TrueConfSettings",
    "WebChannel",
    "build_channels",
]
