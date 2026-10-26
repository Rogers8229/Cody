"""Typed configuration with hard validation.

A risk limit that can be silently set to something insane is not a limit. Every
field is validated on load; a bad config fails before a single order is sent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ConfigError(msg)


@dataclass
class RiskConfig:
    # --- sizing: fixed fraction of the CURRENT book, never the last trade ---
    risk_per_trade_pct: float = 0.02
    max_position_pct: float = 0.05
    max_gross_exposure_pct: float = 0.60
    max_liquidity_frac: float = 0.01  # never take >1% of pool liquidity

    # --- contract gates: reject the order, do not merely lower the score ---
    max_top10_holder_pct: float = 0.25
    min_liquidity_usd: float = 25_000.0
    require_mint_authority_revoked: bool = True
    require_lp_locked: bool = True
    reject_honeypot: bool = True
    max_token_age_s: float = 3600.0

    # --- data integrity ---
    max_feed_lag_s: float = 5.0

    # --- spy ---
    max_chase_pct: float = 0.15
    max_allocation_per_wallet_pct: float = 0.10
    wallet_demote_after_losses: int = 3

    # --- exits ---
    stop_loss_pct: float = 0.25
    take_profit_pct: float = 0.60
    trailing_stop_pct: float = 0.20
    max_hold_s: float = 3600.0

    # --- engineering safety factor (never run at rated limit) ---
    safety_factor: float = 2.0

    # --- circuit breakers ---
    consecutive_loss_limit: int = 3
    consecutive_loss_cooldown_s: float = 900.0
    drawdown_halt_pct: float = 0.20
    daily_loss_halt_pct: float = 0.10
    liquidity_drop_pct: float = 0.40
    liquidity_drop_window_s: float = 120.0

    def validate(self) -> None:
        pct_fields = [
            "risk_per_trade_pct", "max_position_pct", "max_gross_exposure_pct",
            "max_liquidity_frac", "max_top10_holder_pct", "max_chase_pct",
            "max_allocation_per_wallet_pct", "stop_loss_pct", "take_profit_pct",
            "trailing_stop_pct", "drawdown_halt_pct", "daily_loss_halt_pct",
            "liquidity_drop_pct",
        ]
        for name in pct_fields:
            v = getattr(self, name)
            _require(0 < v <= 1.0, f"{name} must be in (0, 1], got {v}")
        _require(self.safety_factor >= 1.0, "safety_factor must be >= 1.0")
        _require(self.risk_per_trade_pct <= self.max_position_pct,
                 "risk_per_trade_pct cannot exceed max_position_pct")
        _require(self.max_gross_exposure_pct <= 1.0,
                 "max_gross_exposure_pct must be <= 1.0")
        _require(self.consecutive_loss_limit >= 1, "consecutive_loss_limit >= 1")
        _require(self.max_feed_lag_s > 0, "max_feed_lag_s must be > 0")


@dataclass
class GamblerConfig:
    enabled: bool = True
    capital_pct: float = 0.40
    launch_window_s: float = 1800.0
    min_score: float = 0.55
    min_buy_pressure: float = 0.55
    min_volume_surge: float = 1.5
    min_change_5m: float = 0.03


@dataclass
class HouseConfig:
    enabled: bool = True
    capital_pct: float = 0.20
    fee_rate: float = 0.02  # 2% of volume
    deploy_cost_usd: float = 1.5
    seed_liquidity_usd: float = 200.0
    gas_cost_usd: float = 0.75
    expected_volume_per_token_usd: float = 18_000.0
    max_launches_per_day: int = 6
    max_open_launches: int = 6  # concurrency cap on seed float
    min_fee_cover_ratio: float = 2.0  # legacy; fee-cover uses risk.safety_factor
    min_edge_multiple: float = 1.15  # fees must clear expected seed loss x this
    launch_cooldown_s: float = 1800.0
    fee_horizon_s: float = 86_400.0
    min_market_volume_5m_usd: float = 5_000.0
    # realistic launch outcomes: most die, a few carry the desk.
    # seed loss is drawn from the same dead/live split as volume so the two are
    # internally consistent; the economics derive E[loss] from these bounds.
    dead_launch_prob: float = 0.40
    dead_volume_frac: float = 0.03
    live_volume_median_frac: float = 0.50
    live_volume_sigma: float = 0.9
    volume_cap_mult: float = 8.0
    dead_seed_loss_lo: float = 0.20
    dead_seed_loss_hi: float = 0.50
    live_seed_loss_lo: float = 0.00
    live_seed_loss_hi: float = 0.08
    # hard stop when fees fall below cost over a rolling window
    fee_stop_window: int = 8
    fee_stop_cooldown_s: float = 21_600.0


@dataclass
class SpyConfig:
    enabled: bool = True
    capital_pct: float = 0.40
    min_wallet_score: float = 0.35
    top_n_wallets: int = 5
    copy_latency_s: float = 12.0
    mirror_exits: bool = True


@dataclass
class ExecutionConfig:
    taker_fee_bps: float = 50.0
    base_slippage_bps: float = 40.0
    impact_coeff: float = 2.0  # extra bps per 1% of pool taken
    latency_s: float = 1.0


@dataclass
class BacktestConfig:
    start_equity_usd: float = 10_000.0
    days: int = 30
    step_s: float = 60.0
    seed: int = 7
    tokens_per_day: float = 12.0
    output_dir: str = "outputs"


@dataclass
class LiveConfig:
    rpc_url: str = ""
    wallet_private_key_env: str = "CODY_WALLET_KEY"
    max_order_usd: float = 250.0
    dry_run: bool = True
    require_confirm: bool = True


@dataclass
class Config:
    risk: RiskConfig = field(default_factory=RiskConfig)
    gambler: GamblerConfig = field(default_factory=GamblerConfig)
    house: HouseConfig = field(default_factory=HouseConfig)
    spy: SpyConfig = field(default_factory=SpyConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    live: LiveConfig = field(default_factory=LiveConfig)

    def validate(self) -> None:
        self.risk.validate()
        total = 0.0
        for s in (self.gambler, self.house, self.spy):
            if s.enabled:
                _require(0 < s.capital_pct <= 1.0, "capital_pct must be in (0,1]")
                total += s.capital_pct
        _require(total <= 1.0 + 1e-9,
                 f"enabled strategy capital_pct sums to {total:.2f} > 1.0")
        _require(self.execution.taker_fee_bps >= 0, "taker_fee_bps >= 0")
        _require(self.backtest.start_equity_usd > 0, "start_equity_usd > 0")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        cfg = cls()
        for section in fields(cls):
            if section.name not in data:
                continue
            sub = getattr(cfg, section.name)
            payload = data[section.name] or {}
            if not isinstance(payload, dict):
                raise ConfigError(f"section {section.name} must be a mapping")
            valid = {f.name for f in fields(sub)}
            unknown = set(payload) - valid
            if unknown:
                raise ConfigError(
                    f"unknown keys in {section.name}: {sorted(unknown)}")
            for k, v in payload.items():
                setattr(sub, k, v)
        cfg.validate()
        return cfg

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"config not found: {p}")
        text = p.read_text()
        if p.suffix in (".yaml", ".yml"):
            try:
                import yaml
            except ImportError as exc:  # pragma: no cover
                raise ConfigError("PyYAML required for YAML configs") from exc
            data = yaml.safe_load(text) or {}
        elif p.suffix == ".json":
            data = json.loads(text)
        else:
            raise ConfigError(f"unsupported config type: {p.suffix}")
        return cls.from_dict(data)
