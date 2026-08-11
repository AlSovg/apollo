"""Провайдер каталога против настоящего OpenLDAP.

Моки библиотеки здесь были бы самообманом: они проверяли бы наше представление
о протоколе, а не работу с каталогом. Контейнер поднимается оверлеем
docker-compose.dev.yml; без него тесты явно пропускаются.
"""

import os

import pytest

from app.adapters.identity import LdapIdentityProvider, LdapSettings
from app.adapters.identity.base import DirectoryUnavailableError
from app.adapters.identity.ldap import format_subject_id

BASE_DN = "dc=apollo,dc=test"
ADMIN_DN = f"cn=admin,{BASE_DN}"
ADMIN_PASSWORD = "ldap-admin-pass"


def ldap_settings(**overrides: object) -> LdapSettings:
    defaults: dict[str, object] = {
        "host": os.environ.get("TEST_LDAP_HOST", "127.0.0.1"),
        "port": int(os.environ.get("TEST_LDAP_PORT", "1389")),
        # На стенде каталог без TLS; в бою LDAPS обязателен (см. LDAP_USE_SSL)
        "use_ssl": False,
        "base_dn": BASE_DN,
        "bind_dn": ADMIN_DN,
        "bind_password": ADMIN_PASSWORD,
        "username_attribute": "uid",
        # В OpenLDAP роль стабильного идентификатора играет entryUUID,
        # в Active Directory — objectGUID
        "id_attribute": "entryUUID",
        "group_object_class": "groupOfNames",
        "ca_certs_file": "",
        "timeout_s": 5,
    }
    defaults.update(overrides)
    return LdapSettings(**defaults)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def directory() -> LdapIdentityProvider:
    provider = LdapIdentityProvider(ldap_settings())
    try:
        with provider._service_connection():
            pass
    except Exception as exc:  # noqa: BLE001 — причина видна в пропуске
        pytest.skip(f"Каталог недоступен ({type(exc).__name__}: {exc})")
    return provider


async def test_authenticate_returns_subject_with_groups(
    directory: LdapIdentityProvider,
) -> None:
    subject = await directory.authenticate("noc.duty", "duty-pass")

    assert subject is not None
    assert subject.username == "noc.duty"
    assert subject.subject_id  # entryUUID
    assert "cn=noc-engineers,ou=groups,dc=apollo,dc=test" in subject.roles
    # Отображаемое имя доступно, но в БД оно не сохраняется
    assert subject.display_name == "Дежурный смены NOC"


async def test_wrong_password_is_rejected(directory: LdapIdentityProvider) -> None:
    assert await directory.authenticate("noc.duty", "не тот пароль") is None


async def test_empty_password_is_rejected(directory: LdapIdentityProvider) -> None:
    """Пустой пароль в LDAP — это анонимный bind, а не успешный вход."""
    assert await directory.authenticate("noc.duty", "") is None


async def test_unknown_user_is_rejected(directory: LdapIdentityProvider) -> None:
    assert await directory.authenticate("нет.такого", "любой") is None


async def test_user_without_groups_has_no_roles(directory: LdapIdentityProvider) -> None:
    subject = await directory.authenticate("no.groups", "nobody-pass")

    assert subject is not None
    assert subject.roles == []


async def test_roles_and_activity_by_subject_id(directory: LdapIdentityProvider) -> None:
    subject = await directory.authenticate("admin.ops", "admin-pass")
    assert subject is not None

    groups = await directory.get_roles(subject.subject_id)
    assert "cn=platform-admins,ou=groups,dc=apollo,dc=test" in groups
    assert await directory.is_active(subject.subject_id) is True


async def test_absent_account_is_inactive(directory: LdapIdentityProvider) -> None:
    """Учётки нет в каталоге → доступ закрывается (FR-IDM-4)."""
    assert await directory.is_active("00000000-0000-0000-0000-000000000000") is False


async def test_describe_returns_display_name(directory: LdapIdentityProvider) -> None:
    subject = await directory.authenticate("biz.owner", "viewer-pass")
    assert subject is not None

    described = await directory.describe(subject.subject_id)
    assert described is not None
    assert described.display_name == "Владелец сервиса"
    assert described.username == "biz.owner"


async def test_unreachable_directory_raises_dedicated_error() -> None:
    """Недоступность каталога отличима от неверного пароля."""
    provider = LdapIdentityProvider(ldap_settings(port=1, timeout_s=1))

    with pytest.raises(DirectoryUnavailableError):
        await provider.authenticate("noc.duty", "duty-pass")


def test_object_guid_is_formatted_as_uuid() -> None:
    raw = bytes.fromhex("78563412341278563412785634127856")
    assert format_subject_id(raw) == "12345678-1234-5678-3412-785634127856"
    assert format_subject_id("уже-строка") == "уже-строка"
