"""Веб-инбокс — основной канал.

Доставка здесь и есть сама строка в notifications: как только она получает
status=sent, уведомление видно в инбоксе через вьюху v_user_inbox. Внешних
систем на пути нет, поэтому канал не может отказать.
"""

from app.adapters.channels.base import NotificationMessage


class WebChannel:
    name = "web"

    async def send(self, message: NotificationMessage, address: str | None) -> None:
        # Адрес не нужен: получатель определяется user_id самой строки
        return None
