from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_api_key
from app.db.base import get_session
from app.db.models import Instrument, Order, OrderSide, OrderType, Trade
from app.services import ledger, trading

# Same shape as app/api/routes.py's router: auth applies to every route in
# this file, and it's registered separately in app/main.py rather than
# folded into the payments router, purely to keep the two domains'
# endpoints in files a reader can hold in their head independently.
router = APIRouter(dependencies=[Depends(require_api_key)])


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

class InstrumentIn(BaseModel):
    symbol: str
    quote_currency: str = "INR"


class InstrumentOut(BaseModel):
    id: uuid.UUID
    symbol: str
    quote_currency: str

    @classmethod
    def of(cls, i: Instrument) -> InstrumentOut:
        return cls(id=i.id, symbol=i.symbol, quote_currency=i.quote_currency)


class OrderIn(BaseModel):
    instrument_id: uuid.UUID
    cash_account_id: uuid.UUID
    side: OrderSide
    order_type: OrderType
    quantity: int = Field(gt=0)
    # Required for a limit order, forbidden for a market order — checked
    # below rather than left to app/services/trading.py, so a malformed
    # request 422s at the API boundary instead of reaching the service
    # layer at all. The DB's own CHECK constraint (migration 0003) is the
    # last line of defense if this is ever bypassed, not the first.
    limit_price_minor: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _limit_price_matches_order_type(self) -> OrderIn:
        if self.order_type is OrderType.LIMIT and self.limit_price_minor is None:
            raise ValueError("limit_price_minor is required for a limit order")
        if self.order_type is OrderType.MARKET and self.limit_price_minor is not None:
            raise ValueError("limit_price_minor must be omitted for a market order")
        return self


class OrderOut(BaseModel):
    id: uuid.UUID
    instrument_id: uuid.UUID
    cash_account_id: uuid.UUID
    side: str
    order_type: str
    limit_price_minor: int | None
    quantity: int
    filled_quantity: int
    status: str
    sequence: int

    @classmethod
    def of(cls, o: Order) -> OrderOut:
        return cls(
            id=o.id, instrument_id=o.instrument_id, cash_account_id=o.cash_account_id,
            side=o.side.value, order_type=o.order_type.value,
            limit_price_minor=o.limit_price_minor,
            quantity=o.quantity, filled_quantity=o.filled_quantity,
            status=o.status.value, sequence=o.sequence,
        )


class TradeOut(BaseModel):
    id: uuid.UUID
    instrument_id: uuid.UUID
    buy_order_id: uuid.UUID
    sell_order_id: uuid.UUID
    price_minor: int
    quantity: int

    @classmethod
    def of(cls, t: Trade) -> TradeOut:
        return cls(
            id=t.id, instrument_id=t.instrument_id,
            buy_order_id=t.buy_order_id, sell_order_id=t.sell_order_id,
            price_minor=t.price_minor, quantity=t.quantity,
        )


# --------------------------------------------------------------------------
# instruments
# --------------------------------------------------------------------------

@router.post("/instruments", response_model=InstrumentOut, status_code=201)
async def create_instrument(body: InstrumentIn, session: AsyncSession = Depends(get_session)):
    async with session.begin():
        instrument = Instrument(**body.model_dump())
        session.add(instrument)
        await session.flush()
        return InstrumentOut.of(instrument)


@router.get("/instruments", response_model=list[InstrumentOut])
async def list_instruments(session: AsyncSession = Depends(get_session)):
    rows = (
        await session.execute(select(Instrument).order_by(Instrument.created_at.desc()))
    ).scalars().all()
    return [InstrumentOut.of(i) for i in rows]


# --------------------------------------------------------------------------
# orders
# --------------------------------------------------------------------------

@router.post("/orders", response_model=OrderOut, status_code=201)
async def create_order(
    body: OrderIn,
    session: AsyncSession = Depends(get_session),
    # Required, not optional — same reasoning as app/api/routes.py's
    # Idempotency-Key: an order retried after a network timeout without one
    # could get placed twice.
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    try:
        async with session.begin():
            order = await trading.place_order(
                session,
                idempotency_key=idempotency_key,
                instrument_id=body.instrument_id,
                cash_account_id=body.cash_account_id,
                side=body.side,
                order_type=body.order_type,
                quantity=body.quantity,
                limit_price_minor=body.limit_price_minor,
            )
    except IntegrityError:
        # Two concurrent requests carrying the same Idempotency-Key both
        # passed place_order()'s initial "does this key exist yet?" check
        # before either had committed — the same race app/api/routes.py's
        # create_payment() handles for payments, closed by the identical
        # database-level unique constraint (uq_orders_idempotency). Read
        # back whichever request won and return ITS order.
        #
        # A DIFFERENT, narrower race (app/services/trading.py's
        # _position_account — two concurrent FIRST orders in a brand-new
        # instrument by the same owner) can also raise IntegrityError here,
        # and has no order row to read back by THIS request's key: the
        # `is None` branch below is what a caller sees for that case — a
        # clean 409 asking them to retry, not the unhandled 500 letting the
        # exception propagate unhandled would have produced.
        existing = (
            await session.execute(select(Order).where(Order.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if existing is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "order could not be placed; retry"
            ) from None
        return OrderOut.of(existing)
    except ledger.InsufficientFunds as exc:
        # 422, not 500 — the request was well-formed; the world said no.
        # Same mapping app/api/routes.py uses for the identical exception.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except (ledger.LedgerError, trading.TradingError) as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    return OrderOut.of(order)


@router.get("/orders", response_model=list[OrderOut])
async def list_orders(
    instrument_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    """
    Newest-first by `sequence` (the same total order the matcher's time
    priority uses — see app/domain/matching.py), not `created_at`: it's a
    real, gap-tolerant, strictly increasing column, not a timestamp two
    orders could tie on.
    """
    query = select(Order).order_by(Order.sequence.desc()).limit(500)
    if instrument_id is not None:
        query = query.where(Order.instrument_id == instrument_id)
    rows = (await session.execute(query)).scalars().all()
    return [OrderOut.of(o) for o in rows]


@router.get("/orders/{order_id}", response_model=OrderOut)
async def get_order(order_id: uuid.UUID, session: AsyncSession = Depends(get_session)):
    order = await session.get(Order, order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "order not found")
    return OrderOut.of(order)


@router.get("/trades", response_model=list[TradeOut])
async def list_trades(
    instrument_id: uuid.UUID | None = None,
    session: AsyncSession = Depends(get_session),
):
    query = select(Trade).order_by(Trade.created_at.desc()).limit(500)
    if instrument_id is not None:
        query = query.where(Trade.instrument_id == instrument_id)
    rows = (await session.execute(query)).scalars().all()
    return [TradeOut.of(t) for t in rows]
