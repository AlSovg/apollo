"""Выбор резолвера контактов по AUTH_PROVIDER."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.contacts.base import ContactResolver
from app.adapters.contacts.ldap import LdapContactResolver
from app.adapters.contacts.local import LocalContactResolver
from app.adapters.identity import ldap_settings
from app.config import Settings


def build_contact_resolver(settings: Settings, session: AsyncSession) -> ContactResolver:
    if settings.auth_provider == "ldap":
        return LdapContactResolver(
            session,
            ldap_settings(settings),
            email_attribute=settings.ldap_email_attribute,
            trueconf_attribute=settings.ldap_trueconf_attribute,
        )
    return LocalContactResolver(session)


__all__ = [
    "ContactResolver",
    "LdapContactResolver",
    "LocalContactResolver",
    "build_contact_resolver",
]
