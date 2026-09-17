"""trading: instruments, orders, trades; ledger currency generalized

Revision ID: 0003
Revises: 0002

See docs/adr/0008-matching-engine.md Decision 4 for why `currency` widens
and its CHECK constraint relaxes — it's reused as "unit of account" so a
position account can be denominated in an instrument's symbol instead of a
real ISO currency code.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- widen currency/unit-of-account columns, relax the CHECK ----------
    op.alter_column("accounts", "currency", type_=sa.String(16))
    op.alter_column("balances", "currency", type_=sa.String(16))
    op.alter_column("ledger_entries", "currency", type_=sa.String(16))

    op.drop_constraint("ck_accounts_currency_iso", "accounts", type_="check")
    op.create_check_constraint(
        "ck_accounts_currency_iso", "accounts", "currency ~ '^[A-Z0-9]{1,16}$'"
    )

    # Guards the race in app/services/trading.py's _position_account():
    # two concurrent first trades in the same instrument by the same owner
    # must not both create a position account. `name` is included so this
    # does NOT constrain ordinary payments accounts (an owner may have more
    # than one wallet in the same currency).
    op.create_unique_constraint(
        "uq_accounts_owner_name_currency", "accounts", ["owner_id", "name", "currency"]
    )

    # --- instruments ---------------------------------------------------
    op.create_table(
        "instruments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("symbol", sa.String(16), nullable=False, unique=True),
        sa.Column("quote_currency", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("symbol ~ '^[A-Z0-9]{1,16}$'", name="ck_instruments_symbol_format"),
        sa.CheckConstraint(
            "quote_currency ~ '^[A-Z]{3}$'", name="ck_instruments_quote_currency_iso"
        ),
    )

    # --- orders -------------------------------------------------------
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False, index=True),
        sa.Column(
            "instrument_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"), nullable=False, index=True,
        ),
        sa.Column(
            "cash_account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id"), nullable=False,
        ),
        sa.Column(
            "position_account_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id"), nullable=False,
        ),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(8), nullable=False),
        sa.Column("limit_price_minor", sa.BigInteger, nullable=True),
        sa.Column("quantity", sa.BigInteger, nullable=False),
        sa.Column("filled_quantity", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        # Time priority's tie-breaker — see app/db/models.py Order.sequence.
        sa.Column(
            "sequence", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_orders_idempotency"),
        sa.CheckConstraint("quantity > 0", name="ck_orders_quantity_positive"),
        sa.CheckConstraint(
            "filled_quantity >= 0 AND filled_quantity <= quantity",
            name="ck_orders_filled_quantity_in_range",
        ),
        # UPPERCASE — see app/db/models.py Order's __table_args__ comment:
        # SQLAlchemy's Enum(native_enum=False) stores the member NAME
        # ("LIMIT"/"MARKET"), not order_type.value ("limit"/"market").
        sa.CheckConstraint(
            "(order_type = 'LIMIT' AND limit_price_minor IS NOT NULL AND limit_price_minor > 0) "
            "OR (order_type = 'MARKET' AND limit_price_minor IS NULL)",
            name="ck_orders_limit_price_matches_type",
        ),
    )

    # --- trades ---------------------------------------------------------
    op.create_table(
        "trades",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "instrument_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"), nullable=False, index=True,
        ),
        sa.Column(
            "buy_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id"),
            nullable=False,
        ),
        sa.Column(
            "sell_order_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("orders.id"),
            nullable=False,
        ),
        sa.Column("price_minor", sa.BigInteger, nullable=False),
        sa.Column("quantity", sa.BigInteger, nullable=False),
        # NULL exactly for a self-match that nets to zero on every account it
        # touches — see docs/adr/0008-matching-engine.md Decision 4.
        sa.Column(
            "ledger_transaction_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ledger_transactions.id"), nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("price_minor > 0", name="ck_trades_price_positive"),
        sa.CheckConstraint("quantity > 0", name="ck_trades_quantity_positive"),
    )


def downgrade() -> None:
    op.drop_table("trades")
    op.drop_table("orders")
    op.drop_table("instruments")

    op.drop_constraint("uq_accounts_owner_name_currency", "accounts", type_="unique")
    op.drop_constraint("ck_accounts_currency_iso", "accounts", type_="check")
    op.create_check_constraint(
        "ck_accounts_currency_iso", "accounts", "currency ~ '^[A-Z]{3}$'"
    )

    op.alter_column("ledger_entries", "currency", type_=sa.String(3))
    op.alter_column("balances", "currency", type_=sa.String(3))
    op.alter_column("accounts", "currency", type_=sa.String(3))
