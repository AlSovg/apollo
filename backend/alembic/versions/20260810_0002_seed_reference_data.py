"""Сид справочников: источники, роли, шкала критичности.

Данные, без которых система не работает: FK raw_events.source ссылается на
event_sources, роли нужны для RBAC, severity_map — для нормализации.

Revision ID: 0002_seed
Revises: 0001_initial
Create Date: 2026-08-10

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_seed"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_CODES = ("zabbix", "solarwinds", "generic")
ROLE_CODES = ("admin", "engineer", "viewer")


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO event_sources (code, name, kind, adapter_class, timezone) VALUES
            ('zabbix',     'Zabbix Server 7.0 (АСУ ТП)',   'adapter', 'ZabbixAdapter',     'Europe/Moscow'),
            ('solarwinds', 'SolarWinds HCO (сеть)',        'adapter', 'SolarWindsAdapter', NULL),
            ('generic',    'Универсальный приём',          'canonical', NULL,              NULL)
        ON CONFLICT (code) DO NOTHING
        """
    )

    op.execute(
        """
        INSERT INTO roles (code, name, description, is_system) VALUES
            ('admin',    'Администратор платформы', 'Темы, подписки, пользователи, аудит', true),
            ('engineer', 'Инженер / дежурный',      'Инциденты, инбокс, свои подписки',    true),
            ('viewer',   'Наблюдатель',             'Только просмотр дашборда',            true)
        ON CONFLICT (code) DO NOTHING
        """
    )

    # Zabbix маппится по числовому event_nseverity — оно надёжнее строки.
    # ВНИМАНИЕ: набор уровней SolarWinds выписан по памяти, сверить с реальной
    # шкалой Orion до боевого использования (открытый вопрос 2 в ТЗ).
    op.execute(
        """
        INSERT INTO severity_map (source, raw_value, severity, label) VALUES
            ('zabbix', '0', 0, 'Not classified'),
            ('zabbix', '1', 1, 'Information'),
            ('zabbix', '2', 2, 'Warning'),
            ('zabbix', '3', 3, 'Average'),
            ('zabbix', '4', 4, 'High'),
            ('zabbix', '5', 5, 'Disaster'),
            ('solarwinds', 'informational', 0, 'Informational'),
            ('solarwinds', 'notice',        1, 'Notice'),
            ('solarwinds', 'warning',       2, 'Warning'),
            ('solarwinds', 'serious',       3, 'Serious'),
            ('solarwinds', 'critical',      4, 'Critical')
        ON CONFLICT (source, raw_value) DO NOTHING
        """
    )


def downgrade() -> None:
    sources = ", ".join(f"'{code}'" for code in SOURCE_CODES)
    roles = ", ".join(f"'{code}'" for code in ROLE_CODES)
    op.execute(f"DELETE FROM severity_map WHERE source IN ({sources})")
    op.execute(f"DELETE FROM roles WHERE code IN ({roles})")
    op.execute(f"DELETE FROM event_sources WHERE code IN ({sources})")
