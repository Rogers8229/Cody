"""Contract checks are gates that reject the order, not scores that lower it."""

from cody.config import RiskConfig
from cody.models import StrategyId
from cody.risk import ChaseGate, ContractGate, ExposureGate, FeedLagGate


def _check(gate, order, snap, pf, prices=None, feed_age=0.0, cfg=None):
    cfg = cfg or RiskConfig()
    prices = prices or {snap.mint: snap.price}
    return gate.check(order, snap, pf, prices, feed_age, cfg)


def test_contract_gate_passes_clean_token(snap_factory, order_factory,
                                         portfolio_factory):
    snap = snap_factory()
    order = order_factory()
    pf = portfolio_factory()
    assert _check(ContractGate(), order, snap, pf).passed


def test_contract_gate_rejects_bad_token(snap_factory, order_factory,
                                         portfolio_factory):
    pf = portfolio_factory()
    order = order_factory()
    gate = ContractGate()

    bad = [
        snap_factory(honeypot=True),
        snap_factory(lp_locked=False),
        snap_factory(mint_authority_revoked=False),
        snap_factory(top10_holder_pct=0.40),
        snap_factory(liquidity_usd=1_000),
        snap_factory(age_s=99_999),
    ]
    for snap in bad:
        res = _check(gate, order, snap, pf)
        assert not res.passed, f"should reject {snap}"
        assert res.reasons


def test_contract_gate_is_a_gate_not_a_score(snap_factory, order_factory,
                                             portfolio_factory):
    """A rejected token must produce no order at all, not a smaller one."""
    snap = snap_factory(honeypot=True)
    res = _check(ContractGate(), order_factory(), snap, portfolio_factory())
    assert res.passed is False
    assert res.reasons


def test_chase_gate_skips_late_copy(snap_factory, order_factory,
                                    portfolio_factory):
    pf = portfolio_factory(strategy=StrategyId.SPY)
    gate = ChaseGate()
    # leader entered at 1.00, now 1.20 -> 20% chase, over the 15% cap
    snap = snap_factory(price=1.20)
    order = order_factory(strategy=StrategyId.SPY,
                          meta={"leader_entry_price": 1.00})
    res = _check(gate, order, snap, pf)
    assert not res.passed
    assert "chase" in res.reasons[0]


def test_chase_gate_allows_acceptable_copy(snap_factory, order_factory,
                                           portfolio_factory):
    pf = portfolio_factory(strategy=StrategyId.SPY)
    snap = snap_factory(price=1.10)
    order = order_factory(strategy=StrategyId.SPY,
                          meta={"leader_entry_price": 1.00})
    assert _check(ChaseGate(), order, snap, pf).passed


def test_feed_lag_gate_rejects_stale_data(snap_factory, order_factory,
                                          portfolio_factory):
    snap = snap_factory()
    res = _check(FeedLagGate(), order_factory(), snap, portfolio_factory(),
                 feed_age=10.0)
    assert not res.passed


def test_exposure_gate_rejects_over_concentration(snap_factory, order_factory,
                                                  portfolio_factory):
    pf = portfolio_factory(equity=1000.0)
    snap = snap_factory()
    # 60% gross cap: a 700 order on a 1000 book must be rejected
    order = order_factory(notional_usd=700.0)
    res = _check(ExposureGate(), order, snap, pf)
    assert not res.passed
    assert "gross exposure" in res.reasons[0]


def test_exposure_gate_rejects_duplicate_mint(snap_factory, order_factory,
                                              portfolio_factory):
    from tests.conftest import add_position
    pf = portfolio_factory(equity=10_000.0)
    add_position(pf, "M1")
    snap = snap_factory(mint="M1")
    order = order_factory(mint="M1", notional_usd=100.0)
    res = _check(ExposureGate(), order, snap, pf)
    assert not res.passed
