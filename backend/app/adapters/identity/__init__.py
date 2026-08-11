"""Выбор провайдера аутентификации по AUTH_PROVIDER.

Переключение ldap ↔ local не требует правок кода — только переменной окружения.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.identity.base import (
    DirectoryUnavailableError,
    IdentityError,
    IdentityProvider,
    Subject,
)
from app.adapters.identity.ldap import (
    LdapIdentityProvider,
    LdapSettings,
    format_subject_id,
)
from app.adapters.identity.local import (
    LocalIdentityProvider,
    hash_password,
    verify_password,
)
from app.config import Settings


def ldap_settings(settings: Settings) -> LdapSettings:
    return LdapSettings(
        host=settings.ldap_host,
        port=settings.ldap_port,
        use_ssl=settings.ldap_use_ssl,
        base_dn=settings.ldap_base_dn,
        bind_dn=settings.ldap_bind_dn,
        bind_password=settings.ldap_bind_password,
        username_attribute=settings.ldap_username_attribute,
        id_attribute=settings.ldap_id_attribute,
        group_object_class=settings.ldap_group_object_class,
        ca_certs_file=settings.ldap_ca_certs_file,
        timeout_s=settings.ldap_timeout_s,
    )


def build_identity_provider(settings: Settings, session: AsyncSession) -> IdentityProvider:
    if settings.auth_provider == "ldap":
        return LdapIdentityProvider(ldap_settings(settings))
    return LocalIdentityProvider(session)


__all__ = [
    "DirectoryUnavailableError",
    "IdentityError",
    "IdentityProvider",
    "LdapIdentityProvider",
    "LdapSettings",
    "LocalIdentityProvider",
    "Subject",
    "build_identity_provider",
    "format_subject_id",
    "hash_password",
    "ldap_settings",
    "verify_password",
]
