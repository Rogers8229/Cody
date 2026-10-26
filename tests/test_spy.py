"""Spy: chase limit, mirror exits, demotion, and wallet-score sanity."""

from cody.config import RiskConfig, SpyConfig
from cody.models import StrategyId, WalletStat
from cody.portfolio import Portfolio
from cody.strategies.base import StrategyContext
from cody.strategies.spy import SpyStrategy
from cody.wallets import LeaderAction
from tests.conftest import add_position


def _win_history(wallet="W1", n=5):
    out = []
    for i in range(n):
        out.append(LeaderAction(wallet, "whale", f"M{i}", f"$M{i}", "enter",
                                i * 100.0, 1.0, 1.0, 1000))
        out.append(LeaderAction(wallet, "whale", f"M{i}", f"$M{i}", "exit",
                                i * 100.0 + 60, 1.2, 1.0, 1000, 0.2))
    return out


def _spy(min_score=0.0, latency=4.0):
    return SpyStrategy(
        SpyConfig(min_wallet_score=min_score, copy_latency_s=latency),
        RiskConfig())


def _ctx(ts, pf, snaps, prices=None):
    return StrategyContext(ts=ts, snapshots=snaps, portfolio=pf,
                           prices=prices or {}, feed_age_s=0.0)


def test_chase_gate_skips_late_copy(snap_factory):
    spy = _spy()
    spy.prime(_win_history())
    spy.on_leader_action(LeaderAction(
        "W1", "whale", "M1", "$M1", "enter", 1000.0, 1.0, 1.0, 1000))
    pf = Portfolio(StrategyId.SPY, 10_000.0, 10_000.0)
    snaps = {"M1": snap_factory(mint="M1", price=1.20)}
    orders = spy.entries(_ctx(1005.0, pf, snaps, {"M1": 1.20}))
    assert orders == []
    assert spy.skipped_chases == 1


def test_spy_copies_inside_chase_limit(snap_factory):
    spy = _spy()
    spy.prime(_win_history())
    spy.on_leader_action(LeaderAction(
        "W1", "whale", "M1", "$M1", "enter", 1000.0, 1.0, 1.0, 1000))
    pf = Portfolio(StrategyId.SPY, 10_000.0, 10_000.0)
    snaps = {"M1": snap_factory(mint="M1", price=1.10)}
    orders = spy.entries(_ctx(1005.0, pf, snaps, {"M1": 1.10}))
    assert len(orders) == 1
    assert orders[0].meta["leader_entry_price"] == 1.0
    assert orders[0].meta["wallet"] == "W1"


def test_spy_mirrors_exits_and_demotes(snap_factory, trade_factory):
    spy = _spy()
    spy.prime(_win_history())
    pf = Portfolio(StrategyId.SPY, 10_000.0, 10_000.0)
    add_position(pf, "M1", price=1.0, qty=1000, wallet="W1")
    spy.on_leader_action(LeaderAction(
        "W1", "whale", "M1", "$M1", "exit", 2000.0, 1.3, 1.0, 1000, 0.3))
    snaps = {"M1": snap_factory(mint="M1", price=1.3)}
    orders = spy.exits(_ctx(2005.0, pf, snaps, {"M1": 1.3}))
    assert len(orders) == 1
    assert orders[0].side.value == "sell"
    assert orders[0].meta["exit_reason"] == "mirror"

    for _ in range(3):
        spy.on_trade_closed(trade_factory(-5.0, wallet="W1"))
    assert "W1" in spy.demoted


def test_wallet_score_penalises_drawdown():
    clean = WalletStat("A", "a", 20, 0.8, 1.0, 60, 0.05, 0)
    ugly = WalletStat("B", "b", 20, 0.8, 1.0, 60, 0.80, 0)
    assert clean.score() > ugly.score()


def test_wallet_score_requires_sample():
    thin = WalletStat("A", "a", 2, 1.0, 2.0, 60, 0.0, 0)
    assert thin.score() == 0.0
