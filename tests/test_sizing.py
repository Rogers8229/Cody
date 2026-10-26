"""The Gambler's signature failure: sizing up after wins (a martingale in a
hoodie). These tests are the build-failing guard for that failure mode."""

from cody.portfolio import fixed_fraction_size


def test_sizing_is_fixed_fraction_never_martingale():
    equity = 1000.0
    sizes = []
    # a winning streak followed by a rug, exactly the tape in the write-up
    for outcome in (0.5, 0.5, 0.5, -0.9, 0.0):
        size = fixed_fraction_size(
            equity=equity, risk_per_trade_pct=0.02, max_position_pct=0.05,
            liquidity_usd=10_000_000, max_liquidity_frac=0.01)
        sizes.append(size)
        equity += size * outcome

    # every size is exactly 2% of the book at the moment it is sized
    assert sizes[0] == 20.0
    assert sizes[1] == 1000.0 * 1.01 * 0.02
    # the size after the rug is smaller than the size before it, not doubled
    assert sizes[4] < sizes[3]
    assert sizes[4] == equity * 0.02


def test_sizing_has_no_last_trade_argument():
    import inspect
    sig = inspect.signature(fixed_fraction_size)
    assert "last_trade" not in sig.parameters
    assert "last_result" not in sig.parameters
    assert "pnl" not in sig.parameters


def test_sizing_respects_concentration_cap():
    size = fixed_fraction_size(
        equity=1000.0, risk_per_trade_pct=0.20, max_position_pct=0.05,
        liquidity_usd=10_000_000, max_liquidity_frac=0.01)
    assert size == 50.0  # 5% cap binds, not the 20% risk budget


def test_sizing_respects_liquidity_cap():
    size = fixed_fraction_size(
        equity=1_000_000.0, risk_per_trade_pct=0.02, max_position_pct=0.05,
        liquidity_usd=100_000.0, max_liquidity_frac=0.01)
    assert size == 1000.0  # 1% of pool binds


def test_sizing_zero_equity_is_zero():
    assert fixed_fraction_size(0.0, 0.02, 0.05, 1e9, 0.01) == 0.0
