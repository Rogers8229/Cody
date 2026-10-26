"""Command line interface.

    python -m cody backtest --config config/default.yaml
    python -m cody fmea
    python -m cody doctor
    python -m cody live --confirm      # guarded, refuses without a signer
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .backtest.simulator import Simulator
from .config import Config, ConfigError
from .report import write_report
from .risk import fmea_table


def _load_config(path: str) -> Config:
    return Config.load(path)


def _print_summary(result) -> None:
    cm = result.combined_metrics
    print("\nCody — backtest summary")
    print("=" * 62)
    print(f"  starting equity : ${cm.starting_equity:,.2f}")
    print(f"  final equity    : ${cm.final_equity:,.2f}")
    print(f"  total return    : {cm.total_return * 100:,.2f}%")
    print(f"  max drawdown    : {cm.max_drawdown * 100:,.2f}%")
    print(f"  sharpe / sortino: {cm.sharpe:.2f} / {cm.sortino:.2f}")
    print(f"  win rate        : {cm.win_rate * 100:.1f}%")
    print(f"  profit factor   : {cm.profit_factor:.2f}")
    print(f"  trades          : {cm.trades}")
    print("-" * 62)
    print(f"  {'strategy':10} {'return':>10} {'maxDD':>8} {'sharpe':>8} "
          f"{'trades':>7} {'end':>12}")
    for sid, r in result.results.items():
        m = r.metrics
        print(f"  {sid.value:10} {m.total_return * 100:9.2f}% "
              f"{m.max_drawdown * 100:7.2f}% {m.sharpe:8.2f} "
              f"{m.trades:7d} ${r.ending_equity:11,.2f}")
    print("=" * 62)


def cmd_backtest(args) -> int:
    cfg = _load_config(args.config)
    if args.days:
        cfg.backtest.days = args.days
    if args.seed is not None:
        cfg.backtest.seed = args.seed
    if args.equity:
        cfg.backtest.start_equity_usd = args.equity
    cfg.validate()
    sim = Simulator(cfg)
    result = sim.run()
    _print_summary(result)
    paths = write_report(result, args.out)
    print("\n  report :", paths["html"])
    print("  json   :", paths["json"])
    print("  trades :", paths["trades"])
    return 0


def cmd_fmea(args) -> int:
    print(f"\n{'bot':9} {'RPN':>5} {'S':>2} {'L':>2} {'D':>2}  failure mode")
    print("-" * 78)
    for f in fmea_table():
        print(f"{f.bot.value:9} {f.rpn:5d} {f.severity:2d} {f.likelihood:2d} "
              f"{f.detectability:2d}  {f.name}")
        print(f"{'':9} mitigation: {f.mitigation}")
        print(f"{'':9} test      : {f.test}\n")
    return 0


def cmd_doctor(args) -> int:
    print("Cody doctor")
    print("-" * 40)
    try:
        cfg = _load_config(args.config)
    except ConfigError as exc:
        print("config: FAIL", exc)
        return 1
    print("config: OK")
    total = sum(s.capital_pct for s in (cfg.gambler, cfg.house, cfg.spy)
                if s.enabled)
    print(f"enabled capital: {total:.0%}")
    print(f"risk per trade : {cfg.risk.risk_per_trade_pct:.1%}")
    print(f"max position   : {cfg.risk.max_position_pct:.1%}")
    print(f"max chase      : {cfg.risk.max_chase_pct:.0%}")
    print(f"safety factor  : {cfg.risk.safety_factor:.1f}x")
    print(f"live dry_run   : {cfg.live.dry_run}")
    print("gates          : contract, chase, feed_lag, exposure")
    print("breakers       : consecutive_loss, drawdown, daily_loss, "
          "feed_lag, liquidity_drop")
    return 0


def cmd_live(args) -> int:
    cfg = _load_config(args.config)
    print("LIVE MODE")
    print("-" * 40)
    if not args.confirm:
        print("refused: pass --confirm to acknowledge real-money risk")
        return 2
    if cfg.live.dry_run:
        print("config has live.dry_run = true; refusing to send real orders.")
        print("set live.dry_run = false only after a paper track record.")
        return 2
    print("live signing is intentionally not bundled. Wire a signer into")
    print("cody.live.executor.LiveExecutor and run the test suite first.")
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cody", description="Memecoin trading desk")
    p.add_argument("--config", default="config/default.yaml")
    sub = p.add_subparsers(dest="cmd")

    b = sub.add_parser("backtest", help="run a synthetic backtest")
    b.add_argument("--days", type=int)
    b.add_argument("--seed", type=int)
    b.add_argument("--equity", type=float)
    b.add_argument("--out", default="outputs")
    b.set_defaults(func=cmd_backtest)

    f = sub.add_parser("fmea", help="print the failure-mode sheet")
    f.set_defaults(func=cmd_fmea)

    d = sub.add_parser("doctor", help="validate config and print limits")
    d.set_defaults(func=cmd_doctor)

    l = sub.add_parser("live", help="guarded live mode")
    l.add_argument("--confirm", action="store_true")
    l.set_defaults(func=cmd_live)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args.func = cmd_backtest
        args.days = None
        args.seed = None
        args.equity = None
        args.out = "outputs"
    try:
        return args.func(args)
    except ConfigError as exc:
        print("config error:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
