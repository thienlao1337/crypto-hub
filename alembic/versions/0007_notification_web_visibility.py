"""notification web visibility

Flag for showing a notification in the panel feed. Needed so the "panel" and "Telegram"
channels can be turned off independently: the notifications table is both the feed and
the send queue, so "Telegram only" can't be expressed without a separate flag.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default is required: the table already has rows, and NOT NULL
    # without a default would fail the migration on the production database.
    # All old notifications used to be shown in the feed - hence true.
    op.add_column(
        'notifications',
        sa.Column('show_web', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # The default was only needed to fill existing rows.
    op.alter_column('notifications', 'show_web', server_default=None)


def downgrade() -> None:
    op.drop_column('notifications', 'show_web')
