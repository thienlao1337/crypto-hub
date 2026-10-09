"""p2p ads and price rules

P2P ads, their pricing rules, the price change log and orders. Plus a separate P2P
permission on the exchange key: on Bybit and Binance these endpoints stay closed until
the account gets advertiser or merchant status, and spot trading permission doesn't
grant access to them.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-06

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '0011'
down_revision: Union[str, None] = '0010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')
BIGPK = sa.BigInteger().with_variant(sa.Integer(), 'sqlite')


def upgrade() -> None:
    op.create_table(
        'p2p_ads',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('exchange_account_id', sa.Integer(), nullable=False),
        sa.Column('external_id', sa.String(length=64), nullable=False),
        sa.Column('side', sa.String(length=8), nullable=False),
        sa.Column('asset', sa.String(length=16), nullable=False),
        sa.Column('fiat', sa.String(length=8), nullable=False),
        sa.Column('price', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('quantity', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('min_amount', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('max_amount', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('payment_methods', JSONB, nullable=True),
        sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw', JSONB, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['exchange_account_id'], ['exchange_accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('exchange_account_id', 'external_id'),
    )
    op.create_index('ix_p2p_ads_account', 'p2p_ads', ['exchange_account_id'], unique=False)

    op.create_table(
        'p2p_price_rules',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ad_id', sa.Integer(), nullable=False),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('target_position', sa.Integer(), nullable=False),
        sa.Column('step', sa.Numeric(precision=36, scale=18), nullable=False),
        sa.Column('floor_pct', sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column('ceiling_pct', sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column('min_competitor_amount', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('min_competitor_rate', sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column('min_change', sa.Numeric(precision=36, scale=18), nullable=False),
        sa.Column('last_applied_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['ad_id'], ['p2p_ads.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ad_id'),
    )

    op.create_table(
        'p2p_price_events',
        sa.Column('id', BIGPK, autoincrement=True, nullable=False),
        sa.Column('ad_id', sa.Integer(), nullable=False),
        sa.Column('event_type', sa.String(length=32), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('price_before', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('price_after', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('competitor_price', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('spot_price', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['ad_id'], ['p2p_ads.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'ix_p2p_price_events_ad_time', 'p2p_price_events', ['ad_id', 'created_at'], unique=False
    )

    op.create_table(
        'p2p_orders',
        sa.Column('id', BIGPK, autoincrement=True, nullable=False),
        sa.Column('exchange_account_id', sa.Integer(), nullable=False),
        sa.Column('ad_id', sa.Integer(), nullable=True),
        sa.Column('external_id', sa.String(length=64), nullable=False),
        sa.Column('side', sa.String(length=8), nullable=False),
        sa.Column('asset', sa.String(length=16), nullable=False),
        sa.Column('fiat', sa.String(length=8), nullable=False),
        sa.Column('amount', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('fiat_amount', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('price', sa.Numeric(precision=36, scale=18), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('counterparty', sa.String(length=128), nullable=True),
        sa.Column('paid_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('release_reason', sa.Text(), nullable=True),
        sa.Column('synced_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('raw', JSONB, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['ad_id'], ['p2p_ads.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['exchange_account_id'], ['exchange_accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('exchange_account_id', 'external_id'),
    )
    op.create_index(
        'ix_p2p_orders_account_time', 'p2p_orders', ['exchange_account_id', 'created_at'],
        unique=False,
    )

    # server_default is required: the table already has keys, and NOT NULL
    # without a default would fail the migration on the production database.
    # Existing keys have no P2P permission - hence false.
    for column in ('requested_p2p', 'allow_p2p'):
        op.add_column(
            'exchange_accounts',
            sa.Column(column, sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        # From now on the application sets the value.
        op.alter_column('exchange_accounts', column, server_default=None)


def downgrade() -> None:
    op.drop_column('exchange_accounts', 'allow_p2p')
    op.drop_column('exchange_accounts', 'requested_p2p')
    op.drop_index('ix_p2p_orders_account_time', table_name='p2p_orders')
    op.drop_table('p2p_orders')
    op.drop_index('ix_p2p_price_events_ad_time', table_name='p2p_price_events')
    op.drop_table('p2p_price_events')
    op.drop_table('p2p_price_rules')
    op.drop_index('ix_p2p_ads_account', table_name='p2p_ads')
    op.drop_table('p2p_ads')
