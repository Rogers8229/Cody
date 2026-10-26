"""End-to-end: determinism, attribution, invariants, and report generation."""

from pathlib import Path

import pytest

from cody.backtest.simulator import Simulator
from cody.config import Config, ConfigError
from cody.models import StrategyId
from cody.report import write_report

ROOT = Path(__file__).resolve().parents[1]


def _cfg() -> Config:
    cfg = Config.load(ROOT / "config" / "default.yaml")
    cfg.backtest.days = 3
    cfg.backtest.step_s = 60
    cfg.backtest.tokens_per_day = 12
    cfg.backtest.seed = 7
    return cfg


def test_backtest_is_deterministic():
    r1 = Simulator(_cfg()).run()
    r2 = Simulator(_cfg()).run()
    assert r1.combined_metrics.final_equity == r2.combined_metrics.final_equity
    assert r1.combined_metrics.trades == r2.combined_metrics.trades


def test_backtest_attribution_and_invariants():
    result = Simulator(_cfg()).run()
    assert set(result.results) == {
        StrategyId.GAMBLER, StrategyId.HOUSE, StrategyId.SPY}
    assert result.combined_metrics.starting_equity == pytest.approx(10_000.0)
    assert len(result.combined_curve) > 100

    house = result.results[StrategyId.HOUSE]
    assert house.extra["holds_supply"] is False
    assert house.extra["break_even_ratio"] > 1.0

    for r in result.results.values():
        assert r.ending_equity > 0
        assert len(r.equity_curve) == len(result.combined_curve)


def test_report_files_written(tmp_path):
    result = Simulator(_cfg()).run()
    paths = write_report(result, tmp_path)
    assert paths["html"].exists()
    assert paths["json"].exists()
    assert paths["trades"].exists()
    html = paths["html"].read_text()
    assert "Failure-mode analysis" in html
    assert "Cody" in html
    assert "Strategy attribution" in html


def test_config_rejects_insane_risk():
    cfg = _cfg()
    cfg.risk.risk_per_trade_pct = 0.9   # 90% per trade
    cfg.risk.max_position_pct = 0.5
    with pytest.raises(ConfigError):
        cfg.validate()


def test_config_rejects_unknown_key():
    with pytest.raises(ConfigError):
        Config.from_dict({"risk": {"not_a_real_key": 1}})
