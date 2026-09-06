"""push subscriptions

Подписки браузеров на веб-пуш и отметка о доставке. Пуш идёт по тому же
признаку show_web, что и лента: это не отдельное событие, а способ
донести до браузера то, что и так попало бы в ленту.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0008'
down_revision: Union[str, None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'push_subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('endpoint', sa.Text(), nullable=False),
        sa.Column('p256dh', sa.String(length=255), nullable=False),
        sa.Column('auth', sa.String(length=255), nullable=False),
        sa.Column('label', sa.String(length=255), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('endpoint'),
    )

    # Старые уведомления пушем не отправляем: пользователь получил бы
    # пачку сообщений о том, что случилось до подписки. Поэтому true.
    op.add_column(
        'notifications',
        sa.Column('delivered_push', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Значение по умолчанию нужно было только для заполнения старых строк:
    # дальше его проставляет приложение, как и у delivered_telegram.
    op.alter_column('notifications', 'delivered_push', server_default=None)


def downgrade() -> None:
    op.drop_column('notifications', 'delivered_push')
    op.drop_table('push_subscriptions')
