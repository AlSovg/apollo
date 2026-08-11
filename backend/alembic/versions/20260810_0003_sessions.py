"""Refresh-сессии: ротация токенов и немедленный отзыв доступа.

Требование FR-IDM-4: при отключении учётки в каталоге сессии отзываются.
Подписанный токен без серверного состояния отозвать нельзя, поэтому появляется
таблица sessions. db/schema.sql обновлён синхронно.

Revision ID: 0003_sessions
Revises: 0002_seed
Create Date: 2026-08-10

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_sessions"
down_revision: str | None = "0002_seed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("jti", sa.Text(), nullable=False, comment="Идентификатор refresh-токена"),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "replaced_by", sa.Text(), nullable=True, comment="jti токена, выданного взамен при ротации"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("jti", name="sessions_jti_uq"),
        comment=(
            "Активные refresh-сессии. Отзыв — проставление revoked_at: деактивация учётки "
            "закрывает доступ сразу, не дожидаясь истечения токена."
        ),
    )
    op.create_index("sessions_user_idx", "sessions", ["user_id", "revoked_at"])


def downgrade() -> None:
    op.drop_index("sessions_user_idx", table_name="sessions")
    op.drop_table("sessions")
