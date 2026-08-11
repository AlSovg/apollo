"""Провайдер каталога (LDAP/AD).

Обязательная интеграция по условиям кейса, поэтому реализация рабочая, а не
заглушка: проверяется против OpenLDAP с воспроизведённой структурой групп.
Переключение на боевой AD — смена переменных окружения, код не меняется.

Пароль пользователя нигде не сохраняется и не кешируется: он используется
ровно один раз, для bind'а от его имени.
"""

import asyncio
import logging
import ssl
import uuid
from typing import Any

from ldap3 import ALL_ATTRIBUTES, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException, LDAPKeyError
from ldap3.utils.conv import escape_filter_chars

from app.adapters.identity.base import (
    DirectoryUnavailableError,
    IdentityError,
    IdentityProvider,
    Subject,
)

logger = logging.getLogger(__name__)


class LdapSettings:
    """Реквизиты подключения к каталогу. Все значения — из окружения."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        use_ssl: bool,
        base_dn: str,
        bind_dn: str,
        bind_password: str,
        username_attribute: str,
        id_attribute: str,
        group_object_class: str,
        ca_certs_file: str,
        timeout_s: int,
    ) -> None:
        self.host = host
        self.port = port
        self.use_ssl = use_ssl
        self.base_dn = base_dn
        self.bind_dn = bind_dn
        self.bind_password = bind_password
        self.username_attribute = username_attribute
        self.id_attribute = id_attribute
        self.group_object_class = group_object_class
        self.ca_certs_file = ca_certs_file
        self.timeout_s = timeout_s


def format_subject_id(value: Any) -> str:
    """objectGUID приходит бинарным, entryUUID — строкой. Приводим к одному виду."""
    if isinstance(value, bytes):
        return str(uuid.UUID(bytes_le=value)) if len(value) == 16 else value.hex()
    return str(value)


class LdapIdentityProvider(IdentityProvider):
    name = "ldap"

    def __init__(self, settings: LdapSettings) -> None:
        self._settings = settings

    # --- подключение ---------------------------------------------------

    def _server(self) -> Server:
        settings = self._settings
        tls = None
        if settings.use_ssl:
            # Проверка сертификата включена: без неё LDAPS не защищает от MITM
            tls = Tls(
                validate=ssl.CERT_REQUIRED,
                ca_certs_file=settings.ca_certs_file or None,
            )
        else:
            logger.warning("ldap: соединение без TLS — допустимо только на стенде")
        return Server(
            settings.host,
            port=settings.port,
            use_ssl=settings.use_ssl,
            tls=tls,
            connect_timeout=settings.timeout_s,
        )

    def _service_connection(self) -> Connection:
        settings = self._settings
        return Connection(
            self._server(),
            user=settings.bind_dn or None,
            password=settings.bind_password or None,
            auto_bind=True,
            receive_timeout=settings.timeout_s,
        )

    # --- операции (ldap3 синхронный, поэтому уходим в поток) ------------

    def _find_entry(self, conn: Connection, ldap_filter: str) -> Any | None:
        conn.search(
            search_base=self._settings.base_dn,
            search_filter=ldap_filter,
            search_scope=SUBTREE,
            # Идентификатор запрашивается отдельно: entryUUID в OpenLDAP —
            # операционный атрибут и в ALL_ATTRIBUTES не попадает
            attributes=[ALL_ATTRIBUTES, self._settings.id_attribute],
        )
        return conn.entries[0] if conn.entries else None

    def _subject_id_of(self, entry: Any) -> str:
        attribute = self._settings.id_attribute
        try:
            value = entry[attribute].value
        except LDAPKeyError as exc:
            raise IdentityError(
                f"каталог не отдаёт атрибут '{attribute}' — проверьте LDAP_ID_ATTRIBUTE"
            ) from exc
        if value is None:
            raise IdentityError(f"у записи каталога пустой атрибут '{attribute}'")
        return format_subject_id(value)

    def _groups_of(self, conn: Connection, entry: Any) -> list[str]:
        """DN групп пользователя.

        memberOf есть в AD и в OpenLDAP с overlay; если его нет — ищем группы
        по вхождению DN пользователя, это работает везде.
        """
        member_of = getattr(entry, "memberOf", None)
        if member_of is not None and member_of.value:
            values = member_of.value
            return [str(values)] if isinstance(values, str) else [str(v) for v in values]

        conn.search(
            search_base=self._settings.base_dn,
            search_filter=(
                f"(&(objectClass={self._settings.group_object_class})"
                f"(member={escape_filter_chars(entry.entry_dn)}))"
            ),
            search_scope=SUBTREE,
            attributes=["cn"],
        )
        return [group.entry_dn for group in conn.entries]

    def _authenticate(self, username: str, password: str) -> Subject | None:
        settings = self._settings
        with self._service_connection() as conn:
            entry = self._find_entry(
                conn,
                f"({settings.username_attribute}={escape_filter_chars(username)})",
            )
            if entry is None:
                return None

            subject_id = self._subject_id_of(entry)
            groups = self._groups_of(conn, entry)
            display_name = getattr(entry, "displayName", None) or getattr(entry, "cn", None)
            user_dn = entry.entry_dn

        # Проверка пароля — отдельный bind от имени пользователя.
        # Пароль в приложении не остаётся: ни в БД, ни в кеше, ни в логах.
        try:
            user_conn = Connection(
                self._server(),
                user=user_dn,
                password=password,
                auto_bind=True,
                receive_timeout=settings.timeout_s,
            )
        except LDAPException:
            return None
        user_conn.unbind()

        return Subject(
            subject_id=subject_id,
            username=username,
            roles=groups,
            display_name=str(display_name.value) if display_name else None,
        )

    def _lookup(self, subject_id: str) -> tuple[Any | None, list[str]]:
        settings = self._settings
        with self._service_connection() as conn:
            entry = self._find_entry(
                conn,
                f"({settings.id_attribute}={escape_filter_chars(subject_id)})",
            )
            if entry is None:
                return None, []
            return entry, self._groups_of(conn, entry)

    # --- публичный контракт --------------------------------------------

    async def authenticate(self, username: str, password: str) -> Subject | None:
        if not password:
            # Пустой пароль в LDAP — это анонимный bind, а не успешный вход
            return None
        try:
            return await asyncio.to_thread(self._authenticate, username, password)
        except LDAPException as exc:
            raise DirectoryUnavailableError(f"каталог недоступен: {type(exc).__name__}") from exc

    async def get_roles(self, subject_id: str) -> list[str]:
        """Возвращает DN групп: во внутренние роли их переводит role_mappings."""
        try:
            _, groups = await asyncio.to_thread(self._lookup, subject_id)
        except LDAPException as exc:
            raise DirectoryUnavailableError(f"каталог недоступен: {type(exc).__name__}") from exc
        return groups

    async def describe(self, subject_id: str) -> Subject | None:
        """Сведения о субъекте для показа в интерфейсе.

        Отображаемое имя берётся здесь и живёт только в ответе API — в БД оно
        не сохраняется (раздел 5 AGENTS.md).
        """
        try:
            entry, groups = await asyncio.to_thread(self._lookup, subject_id)
        except LDAPException as exc:
            raise DirectoryUnavailableError(f"каталог недоступен: {type(exc).__name__}") from exc
        if entry is None:
            return None

        display_name = getattr(entry, "displayName", None) or getattr(entry, "cn", None)
        username = getattr(entry, self._settings.username_attribute, None)
        return Subject(
            subject_id=subject_id,
            username=str(username.value) if username else "",
            roles=groups,
            display_name=str(display_name.value) if display_name else None,
        )

    async def is_active(self, subject_id: str) -> bool:
        """Учётка отсутствует или отключена в каталоге → доступ закрывается."""
        try:
            entry, _ = await asyncio.to_thread(self._lookup, subject_id)
        except LDAPException as exc:
            raise DirectoryUnavailableError(f"каталог недоступен: {type(exc).__name__}") from exc
        if entry is None:
            return False

        # AD помечает отключённые учётки битом 2 в userAccountControl.
        # В OpenLDAP такого атрибута нет — там достаточно факта существования.
        control = getattr(entry, "userAccountControl", None)
        if control is not None and control.value is not None:
            return not bool(int(control.value) & 0x2)
        return True
