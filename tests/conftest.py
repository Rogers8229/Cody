import pytest

from cody.models import (
    ClosedTrade,
    ExitReason,
    Fill,
    Order,
    Position,
    Side,
    StrategyId,
    TokenSnapshot,
)
from cody.portfolio import Portfolio


def make_snap(**kw) -> TokenSnapshot:
    defaults = dict(
        mint="M1", symbol="$M1", ts=1000.0, price=0.001,
        liquidity_usd=100_000.0, volume_24h_usd=1_000_000.0,
        volume_5m_usd=20_000.0, top10_holder_pct=0.15,
        mint_authority_revoked=True, lp_locked=True, honeypot=False,
        age_s=600.0, buy_pressure=0.7, price_5m_ago=0.0009,
        price_1h_ago=0.0008, holder_count=500,
    )
    defaults.update(kw)
    return TokenSnapshot(**defaults)


def make_order(**kw) -> Order:
    defaults = dict(
        strategy=StrategyId.GAMBLER, mint="M1", symbol="$M1", side=Side.BUY,
        notional_usd=100.0, ts=1000.0, ref_price=0.001, reason="test",
        meta={},
    )
    defaults.update(kw)
    return Order(**defaults)


def make_portfolio(equity: float = 10_000.0,
                   strategy: StrategyId = StrategyId.GAMBLER) -> Portfolio:
    return Portfolio(strategy, equity, equity)


def add_position(pf: Portfolio, mint: str = "M1", price: float = 1.0,
                 qty: float = 1000.0, wallet: str | None = None) -> Position:
    pos = Position(
        mint=mint, symbol=f"${mint}", strategy=pf.strategy, qty=qty,
        avg_price=price, opened_ts=0.0, entry_notional=price * qty,
        peak_price=price, stop_price=price * 0.75,
        take_profit_price=price * 1.6, wallet=wallet,
    )
    pf.positions[mint] = pos
    return pos


def make_trade(pnl: float, wallet: str | None = None) -> ClosedTrade:
    return ClosedTrade(
        mint="M1", symbol="$M1", strategy=StrategyId.SPY, qty=100,
        entry_price=1.0, exit_price=1.0 + pnl / 100.0, entry_ts=0.0,
        exit_ts=60.0, pnl_usd=pnl, pnl_pct=pnl / 100.0, fees_usd=0.0,
        exit_reason=ExitReason.STOP, wallet=wallet,
    )


@pytest.fixture
def snap_factory():
    return make_snap


@pytest.fixture
def order_factory():
    return make_order


@pytest.fixture
def portfolio_factory():
    return make_portfolio


@pytest.fixture
def trade_factory():
    return make_trade
