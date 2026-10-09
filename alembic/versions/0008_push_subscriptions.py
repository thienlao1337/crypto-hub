"""push subscriptions

Browser web-push subscriptions and a delivery marker. Push follows the same show_web
flag as the feed: it isn't a separate event, just a way to bring to the browser what
would have landed in the feed anyway.

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

    # Old notifications are not pushed: the user would get a batch of messages
    # about things that happened before subscribing. Hence true.
    op.add_column(
        'notifications',
        sa.Column('delivered_push', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # The default was only needed to fill existing rows: from now on the
    # application sets the value, same as delivered_telegram.
    op.alter_column('notifications', 'delivered_push', server_default=None)


def downgrade() -> None:
    op.drop_column('notifications', 'delivered_push')
    op.drop_table('push_subscriptions')
