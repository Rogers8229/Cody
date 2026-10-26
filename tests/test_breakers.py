"""Every circuit breaker must fire. If one silently stops firing, the build
fails — that is the whole point of the breaker test convention."""

from cody.risk import (
    ConsecutiveLossBreaker,
    DailyLossBreaker,
    DrawdownBreaker,
    FeedLagBreaker,
    LiquidityDropBreaker,
)
from tests.conftest import add_position


def test_consecutive_loss_breaker_fires(trade_factory, portfolio_factory):
    pf = portfolio_factory()
    b = ConsecutiveLossBreaker(limit=3, cooldown_s=900)
    for _ in range(3):
        b.record_trade(trade_factory(-10.0))
    status = b.evaluate(100.0, pf, {}, 0.0)
    assert status.triggered
    assert "consecutive" in status.reason
    assert status.until_ts == 960.0
    # still halted inside the cooldown
    assert b.evaluate(500.0, pf, {}, 0.0).triggered
    # after cooldown it releases and allows a fresh probe (no deadlock)
    assert not b.evaluate(1000.0, pf, {}, 0.0).triggered
    assert b.streak == 0


def test_consecutive_loss_breaker_resets_on_win(trade_factory, portfolio_factory):
    pf = portfolio_factory()
    b = ConsecutiveLossBreaker(limit=3, cooldown_s=900)
    b.record_trade(trade_factory(-10.0))
    b.record_trade(trade_factory(-10.0))
    b.record_trade(trade_factory(5.0))
    assert b.streak == 0
    assert not b.evaluate(1000.0, pf, {}, 0.0).triggered


def test_drawdown_breaker_fires(portfolio_factory):
    pf = portfolio_factory(equity=10_000.0)
    pf.cash = 5_000.0
    add_position(pf, "M1", price=1.0, qty=5_000.0)
    pf.mark({"M1": 1.0})          # peak = 10k
    b = DrawdownBreaker(max_dd_pct=0.20)
    status = b.evaluate(1000.0, pf, {"M1": 0.5}, 0.0)  # equity 7.5k, -25%
    assert status.triggered
    assert "drawdown" in status.reason


def test_daily_loss_breaker_fires(portfolio_factory):
    pf = portfolio_factory(equity=10_000.0)
    pf.cash = 5_000.0
    add_position(pf, "M1", price=1.0, qty=5_000.0)
    pf.day_start_equity = 10_000.0
    b = DailyLossBreaker(max_daily_loss_pct=0.10)
    status = b.evaluate(1000.0, pf, {"M1": 0.7}, 0.0)  # 8.5k, -15%
    assert status.triggered
    assert "daily" in status.reason


def test_feed_lag_breaker_fires(portfolio_factory):
    pf = portfolio_factory()
    b = FeedLagBreaker(max_lag_s=5.0)
    assert not b.evaluate(1000.0, pf, {}, 1.0).triggered
    status = b.evaluate(1000.0, pf, {}, 12.0)
    assert status.triggered
    assert "stale" in status.reason


def test_liquidity_drop_breaker_fires(portfolio_factory):
    pf = portfolio_factory()
    add_position(pf, "M1")
    b = LiquidityDropBreaker(drop_pct=0.40, window_s=120)
    b.observe("M1", 1000.0, 100_000.0)
    b.observe("M1", 1060.0, 50_000.0)   # -50% inside the window
    status = b.evaluate(1060.0, pf, {"M1": 1.0}, 0.0)
    assert status.triggered
    assert "liquidity" in status.reason


def test_liquidity_drop_breaker_ignores_tokens_not_held(portfolio_factory):
    pf = portfolio_factory()
    b = LiquidityDropBreaker(drop_pct=0.40, window_s=120)
    b.observe("M1", 1000.0, 100_000.0)
    b.observe("M1", 1060.0, 10_000.0)
    assert not b.evaluate(1060.0, pf, {}, 0.0).triggered
