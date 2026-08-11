"""Основной резолвер — каталог.

Адрес запрашивается по subject_id в момент отправки и не персистится:
именно поэтому email отсутствует в таблице users.
"""

import asyncio
import logging
from typing import Any

from ldap3 import SUBTREE, Connection, Server
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity import LdapSettings
from app.models.identity import User

logger = logging.getLogger(__name__)


class LdapContactResolver:
    name = "ldap"

    def __init__(
        self,
        session: AsyncSession,
        settings: LdapSettings,
        *,
        email_attribute: str = "mail",
        trueconf_attribute: str = "ipPhone",
    ) -> None:
        self._session = session
        self._settings = settings
        self._attributes = {"email": email_attribute, "trueconf": trueconf_attribute}

    def _lookup(self, subject_id: str, attribute: str) -> str | None:
        settings = self._settings
        server = Server(
            settings.host,
            port=settings.port,
            use_ssl=settings.use_ssl,
            connect_timeout=settings.timeout_s,
        )
        with Connection(
            server,
            user=settings.bind_dn or None,
            password=settings.bind_password or None,
            auto_bind=True,
            receive_timeout=settings.timeout_s,
        ) as conn:
            conn.search(
                search_base=settings.base_dn,
                search_filter=f"({settings.id_attribute}={escape_filter_chars(subject_id)})",
                search_scope=SUBTREE,
                attributes=[attribute],
            )
            if not conn.entries:
                return None
            value: Any = getattr(conn.entries[0], attribute, None)
            return str(value.value) if value and value.value else None

    async def resolve(self, user_id: int, channel: str) -> str | None:
        if channel == "web":
            return None
        attribute = self._attributes.get(channel)
        if attribute is None:
            return None

        subject_id = (
            await self._session.execute(select(User.subject_id).where(User.id == user_id))
        ).scalar_one_or_none()
        if subject_id is None:
            return None

        try:
            return await asyncio.to_thread(self._lookup, subject_id, attribute)
        except LDAPException:
            # Недоступность каталога — временная проблема: уведомление уйдёт в ретрай
            logger.warning("contacts: каталог недоступен", extra={"channel": channel})
            return None
