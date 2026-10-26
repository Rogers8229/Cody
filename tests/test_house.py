"""House economics: break-even before launch, never holds supply, daily budget."""

from cody.config import HouseConfig, RiskConfig
from cody.models import StrategyId
from cody.portfolio import Portfolio
from cody.strategies.base import StrategyContext
from cody.strategies.house import HouseStrategy


def _ctx(ts, pf, snapshots, prices=None):
    return StrategyContext(ts=ts, snapshots=snapshots, portfolio=pf,
                           prices=prices or {}, feed_age_s=0.0)


def test_house_break_even_gate():
    ok = HouseStrategy(HouseConfig(), RiskConfig())
    # two independent gates: fee cover on operating cost x safety factor,
    # and net edge over expected seed loss
    assert ok.fee_cover_ratio() >= 2.0
    assert ok.net_edge_ratio() >= ok.cfg.min_edge_multiple
    assert ok.break_even_ratio() > 1.0
    assert ok.break_even_ok()

    bad = HouseStrategy(
        HouseConfig(expected_volume_per_token_usd=2_000), RiskConfig())
    assert bad.net_edge_ratio() < bad.cfg.min_edge_multiple
    assert not bad.break_even_ok()


def test_house_never_holds_supply(snap_factory):
    pf = Portfolio(StrategyId.HOUSE, 10_000.0, 10_000.0)
    h = HouseStrategy(HouseConfig(), RiskConfig())
    snaps = {"M1": snap_factory(volume_5m_usd=20_000)}
    h.on_tick(_ctx(1000.0, pf, snaps))

    assert len(h.launches) == 1
    assert not h.holds_supply(pf)
    assert pf.positions == {}
    # cost left the book; no token position was created
    assert pf.cash == 10_000.0 - h.capital_per_launch


def test_house_refuses_launch_when_break_even_fails(snap_factory):
    pf = Portfolio(StrategyId.HOUSE, 10_000.0, 10_000.0)
    h = HouseStrategy(
        HouseConfig(expected_volume_per_token_usd=2_000), RiskConfig())
    snaps = {"M1": snap_factory(volume_5m_usd=20_000)}
    h.on_tick(_ctx(1000.0, pf, snaps))
    assert h.launches == []
    assert pf.cash == 10_000.0


def test_house_refuses_launch_when_market_volume_dries_up(snap_factory):
    pf = Portfolio(StrategyId.HOUSE, 10_000.0, 10_000.0)
    h = HouseStrategy(HouseConfig(), RiskConfig())
    snaps = {"M1": snap_factory(volume_5m_usd=100)}
    h.on_tick(_ctx(1000.0, pf, snaps))
    assert h.launches == []


def test_house_daily_budget(snap_factory):
    pf = Portfolio(StrategyId.HOUSE, 10_000.0, 10_000.0)
    cfg = HouseConfig(max_launches_per_day=2, launch_cooldown_s=0)
    h = HouseStrategy(cfg, RiskConfig())
    snaps = {"M1": snap_factory(volume_5m_usd=20_000)}
    for ts in (1000.0, 2000.0, 3000.0, 4000.0):
        h.on_tick(_ctx(ts, pf, snaps))
    assert len(h.launches) == 2


def test_house_accrues_fee_income(snap_factory):
    pf = Portfolio(StrategyId.HOUSE, 10_000.0, 10_000.0)
    h = HouseStrategy(HouseConfig(max_launches_per_day=1), RiskConfig())
    snaps = {"M1": snap_factory(volume_5m_usd=20_000)}
    h.on_tick(_ctx(1000.0, pf, snaps))
    cash_after_launch = pf.cash
    h.on_tick(_ctx(1000.0 + 3600.0, pf, snaps))
    assert h.realized_fees_usd > 0
    assert pf.cash > cash_after_launch


def test_house_seed_booked_at_face_then_loss_realized_at_close(snap_factory):
    """Seed leaves cash at launch but is carried at face in locked_capital; the
    loss is booked only when the launch closes, not front-loaded."""
    pf = Portfolio(StrategyId.HOUSE, 10_000.0, 10_000.0)
    h = HouseStrategy(HouseConfig(max_launches_per_day=1,
                                  launch_cooldown_s=10**9), RiskConfig())
    snaps = {"M1": snap_factory(volume_5m_usd=20_000)}
    h.on_tick(_ctx(1000.0, pf, snaps))
    assert len(h.launches) == 1
    # cash down by full capital out, but seed is carried as recoverable
    assert pf.cash == 10_000.0 - h.capital_per_launch
    assert pf.locked_capital == h.cfg.seed_liquidity_usd
    # equity has only dropped by operating cost, not the whole seed
    eq = pf.equity({})
    assert eq == 10_000.0 - h.operating_cost_per_launch

    launch = h.launches[0]
    # force the launch past its horizon and settle
    h.on_tick(_ctx(1000.0 + launch.horizon_s + 1.0, pf, snaps))
    assert launch.seed_returned
    assert pf.locked_capital == 0.0
    expected_loss = h.cfg.seed_liquidity_usd * launch.realized_seed_loss_pct
    assert h.realized_seed_loss_usd == expected_loss


def test_house_concurrency_cap(snap_factory):
    pf = Portfolio(StrategyId.HOUSE, 10_000.0, 10_000.0)
    cfg = HouseConfig(max_launches_per_day=50, max_open_launches=2,
                      launch_cooldown_s=0)
    h = HouseStrategy(cfg, RiskConfig())
    snaps = {"M1": snap_factory(volume_5m_usd=20_000)}
    for ts in range(1, 10):
        h.on_tick(_ctx(float(ts) * 100.0, pf, snaps))
    assert h.open_launches() == 2
    assert len(h.launches) == 2


def test_house_fee_stop_does_not_thrash(snap_factory):
    """Once the fee-stop trips it must not re-increment every tick."""
    pf = Portfolio(StrategyId.HOUSE, 10_000.0, 10_000.0)
    cfg = HouseConfig(max_launches_per_day=50, max_open_launches=50,
                      launch_cooldown_s=0, fee_stop_window=1,
                      fee_stop_cooldown_s=10_000.0)
    h = HouseStrategy(cfg, RiskConfig())
    snaps = {"M1": snap_factory(volume_5m_usd=20_000)}
    # force a closed losing launch into the book
    h.on_tick(_ctx(1.0, pf, snaps))
    assert h.launches
    l = h.launches[0]
    l.closed = True
    l.accrued_fees_usd = 0.0
    before = h.fee_stops
    for ts in range(2, 40):
        h.on_tick(_ctx(float(ts) * 100.0, pf, snaps))
    # at most one increment per newly-closed launch, never once per tick
    assert h.fee_stops - before <= 1
