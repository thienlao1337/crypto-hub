"""strategy last signal watermark

Marker for "signals up to this id have already been reviewed". Without it every
background pass re-processed the same signals and logged the same rejections: thirty
identical lines piled up in half an hour.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0010'
down_revision: Union[str, None] = '0009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # No foreign key on purpose: this is a watermark, not a reference to a row.
    # Deleting an old signal must not reset the marker and make the strategy
    # reconsider everything.
    op.add_column('strategies', sa.Column('last_signal_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('strategies', 'last_signal_id')
