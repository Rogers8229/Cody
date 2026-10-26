"""Reporting: a self-contained HTML dashboard plus JSON and CSV exports."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .backtest.simulator import BacktestResult, StrategyResult
from .models import StrategyId
from .risk import fmea_table

COLORS = {
    StrategyId.GAMBLER: "#f5a524",
    StrategyId.HOUSE: "#a78bfa",
    StrategyId.SPY: "#22d3ee",
}
LABELS = {
    StrategyId.GAMBLER: "Gambler",
    StrategyId.HOUSE: "House",
    StrategyId.SPY: "Spy",
}


def _fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v * 100:,.2f}%"


def _svg(series: list[tuple[str, str, list[tuple[float, float]]]],
         width: int = 960, height: int = 340) -> str:
    all_vals = [v for _, _, pts in series for _, v in pts]
    if not all_vals:
        return "<svg></svg>"
    lo, hi = min(all_vals), max(all_vals)
    if hi <= lo:
        hi = lo + 1
    pad = (hi - lo) * 0.08
    lo -= pad
    hi += pad
    n = max(len(pts) for _, _, pts in series)
    left, right, top, bottom = 64, 16, 16, 28
    w = width - left - right
    h = height - top - bottom

    def x(i: int) -> float:
        return left + (w * i / max(1, n - 1))

    def y(v: float) -> float:
        return top + h * (1 - (v - lo) / (hi - lo))

    parts = [f'<svg viewBox="0 0 {width} {height}" '
             f'xmlns="http://www.w3.org/2000/svg" role="img">']
    # gridlines
    for g in range(5):
        gy = top + h * g / 4
        val = hi - (hi - lo) * g / 4
        parts.append(f'<line x1="{left}" y1="{gy:.1f}" x2="{width - right}" '
                     f'y2="{gy:.1f}" stroke="#243044" stroke-width="1"/>')
        parts.append(f'<text x="{left - 8}" y="{gy + 4:.1f}" fill="#7c8aa5" '
                     f'font-size="11" text-anchor="end">{_fmt_money(val)}</text>')
    for label, color, pts in series:
        if not pts:
            continue
        stride = max(1, len(pts) // 600)
        sample = pts[::stride]
        coords = " ".join(f"{x(i * stride):.1f},{y(v):.1f}"
                          for i, (_, v) in enumerate(sample))
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.2" '
                     f'stroke-linejoin="round" points="{coords}"/>')
    parts.append("</svg>")
    return "".join(parts)


def _legend(series: list[tuple[str, str, list[tuple[float, float]]]]) -> str:
    items = "".join(
        f'<span class="legend-item"><i style="background:{c}"></i>{label}</span>'
        for label, c, _ in series)
    return f'<div class="legend">{items}</div>'


def _metrics_row(label: str, r: StrategyResult) -> str:
    m = r.metrics
    is_house = r.strategy is StrategyId.HOUSE
    # the House earns fees, not closed positions -- show launches, not trades
    win_cell = "&mdash;" if is_house else f"{m.win_rate * 100:.1f}%"
    pf_cell = "&mdash;" if is_house else f"{m.profit_factor:.2f}"
    trades_cell = (f"{r.extra.get('launches', 0)} launches" if is_house
                   else f"{m.trades}")
    return (
        "<tr>"
        f'<td class="strat"><i style="background:{COLORS[r.strategy]}"></i>'
        f"{label}</td>"
        f"<td>{_fmt_money(r.starting_equity)}</td>"
        f"<td>{_fmt_money(r.ending_equity)}</td>"
        f'<td class="{"pos" if m.total_return >= 0 else "neg"}">'
        f"{_fmt_pct(m.total_return)}</td>"
        f"<td>{_fmt_pct(m.max_drawdown)}</td>"
        f"<td>{m.sharpe:.2f}</td>"
        f"<td>{m.sortino:.2f}</td>"
        f"<td>{win_cell}</td>"
        f"<td>{pf_cell}</td>"
        f"<td>{trades_cell}</td>"
        f"<td>{_fmt_money(m.fees_usd)}</td>"
        "</tr>")


def _fmea_rows() -> str:
    rows = []
    for f in fmea_table():
        cls = "hot" if f.rpn >= 400 else ("warm" if f.rpn >= 250 else "")
        rows.append(
            "<tr>"
            f"<td>{LABELS[f.bot]}</td>"
            f"<td>{f.name}</td>"
            f"<td>{f.severity}</td><td>{f.likelihood}</td>"
            f"<td>{f.detectability}</td>"
            f'<td class="{cls}"><b>{f.rpn}</b></td>'
            f"<td>{f.mitigation}</td>"
            f'<td><code>{f.test}</code></td>'
            "</tr>")
    return "".join(rows)


def _rejections(result: BacktestResult) -> str:
    rows = []
    for sid, r in result.results.items():
        if not r.rejected:
            continue
        items = ", ".join(f"{k}: {v}" for k, v in
                          sorted(r.rejected.items(), key=lambda x: -x[1]))
        rows.append(f"<tr><td>{LABELS[sid]}</td><td>{items}</td></tr>")
    return "".join(rows) or '<tr><td colspan="2">none</td></tr>'


def render_html(result: BacktestResult) -> str:
    series = [("Combined", "#e2e8f0", result.combined_curve)]
    for sid, r in result.results.items():
        series.append((LABELS[sid], COLORS[sid], r.equity_curve))
    svg = _svg(series)
    legend = _legend(series)

    rows = "".join(_metrics_row(LABELS[sid], r)
                   for sid, r in result.results.items())
    cm = result.combined_metrics

    extras = []
    for sid, r in result.results.items():
        if not r.extra:
            continue
        extras.append(f"<h3>{LABELS[sid]}</h3><ul>")
        for k, v in r.extra.items():
            extras.append(f"<li><b>{k}</b>: {v}</li>")
        extras.append("</ul>")
    extras_html = "".join(extras)

    days = (result.end_ts - result.start_ts) / 86_400.0
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cody — Memecoin Trading Desk</title>
<style>
:root{{--bg:#0b0f17;--panel:#121a27;--line:#243044;--text:#e2e8f0;
--muted:#7c8aa5;--pos:#34d399;--neg:#f87171;--accent:#22d3ee}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);
font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
.wrap{{max-width:1120px;margin:0 auto;padding:32px 20px 64px}}
header{{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;
border-bottom:1px solid var(--line);padding-bottom:18px;margin-bottom:26px}}
h1{{font-size:26px;margin:0;letter-spacing:-.02em}}
.badge{{font-size:12px;color:var(--accent);border:1px solid var(--accent);
border-radius:999px;padding:3px 10px;text-transform:uppercase;letter-spacing:.08em}}
.sub{{color:var(--muted);font-size:13px;margin-left:auto}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:12px;margin-bottom:24px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;
padding:14px 16px}}
.card .k{{color:var(--muted);font-size:11px;text-transform:uppercase;
letter-spacing:.08em}}
.card .v{{font-size:22px;font-weight:650;margin-top:6px;
font-variant-numeric:tabular-nums}}
.panel{{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:18px;margin-bottom:22px}}
.panel h2{{font-size:15px;margin:0 0 14px;letter-spacing:.02em}}
.legend{{display:flex;gap:18px;flex-wrap:wrap;margin-top:10px;font-size:12px;
color:var(--muted)}}
.legend-item{{display:flex;align-items:center;gap:7px}}
.legend-item i{{width:10px;height:10px;border-radius:2px;display:inline-block}}
table{{width:100%;border-collapse:collapse;font-size:13px;
font-variant-numeric:tabular-nums}}
th{{text-align:right;color:var(--muted);font-weight:600;font-size:11px;
text-transform:uppercase;letter-spacing:.05em;padding:8px 10px;
border-bottom:1px solid var(--line);white-space:nowrap}}
td{{text-align:right;padding:9px 10px;border-bottom:1px solid #1b2534}}
th:first-child,td:first-child{{text-align:left}}
td.strat{{display:flex;align-items:center;gap:8px}}
td.strat i{{width:9px;height:9px;border-radius:2px;display:inline-block}}
.pos{{color:var(--pos)}} .neg{{color:var(--neg)}}
.hot{{color:#f87171}} .warm{{color:#fbbf24}}
code{{font-size:11px;color:var(--muted);background:#0b0f17;padding:2px 5px;
border-radius:4px}}
ul{{margin:6px 0 14px;padding-left:20px;font-size:13px;color:var(--muted)}}
ul b{{color:var(--text)}}
.foot{{color:var(--muted);font-size:11px;line-height:1.6;border-top:1px solid
var(--line);padding-top:16px}}
</style></head><body><div class="wrap">
<header>
<h1>Cody — Memecoin Trading Desk</h1>
<span class="badge">Backtest</span>
<span class="sub">{days:.0f}-day tape · step {result.step_s:.0f}s ·
seed {result.config.backtest.seed}</span>
</header>

<div class="cards">
<div class="card"><div class="k">Starting</div><div class="v">
{_fmt_money(cm.starting_equity)}</div></div>
<div class="card"><div class="k">Final</div><div class="v">
{_fmt_money(cm.final_equity)}</div></div>
<div class="card"><div class="k">Total return</div>
<div class="v {'pos' if cm.total_return >= 0 else 'neg'}">
{_fmt_pct(cm.total_return)}</div></div>
<div class="card"><div class="k">Max drawdown</div><div class="v">
{_fmt_pct(cm.max_drawdown)}</div></div>
<div class="card"><div class="k">Sharpe</div><div class="v">
{cm.sharpe:.2f}</div></div>
<div class="card"><div class="k">Trades</div><div class="v">
{cm.trades}</div></div>
</div>

<div class="panel"><h2>Equity curve</h2>{svg}{legend}</div>

<div class="panel"><h2>Strategy attribution</h2>
<table><thead><tr><th>Strategy</th><th>Start</th><th>End</th><th>Return</th>
<th>Max DD</th><th>Sharpe</th><th>Sortino</th><th>Win</th><th>PF</th>
<th>Trades</th><th>Fees</th></tr></thead>
<tbody>{rows}</tbody></table></div>

<div class="panel"><h2>Failure-mode analysis — fix the worst RPN first</h2>
<table><thead><tr><th>Bot</th><th>Failure mode</th><th>Sev</th><th>Lik</th>
<th>Det</th><th>RPN</th><th>Mitigation</th><th>Build-failing test</th></tr>
</thead><tbody>{_fmea_rows()}</tbody></table></div>

<div class="panel"><h2>Orders rejected by the gate stack</h2>
<table><thead><tr><th>Strategy</th><th>Reason counts</th></tr></thead>
<tbody>{_rejections(result)}</tbody></table></div>

<div class="panel"><h2>Strategy internals</h2>{extras_html}</div>

<div class="foot">
Synthetic tape for research and engineering validation. Past or simulated
performance does not predict live results. Memecoins are still memecoins: rugs,
honeypots, and liquidity that vanishes in a block do not care how clean your
circuit breakers are. The rules cap the damage; they do not remove it. This is
not financial advice.
</div>
</div></body></html>"""


def write_json(result: BacktestResult, path: str | Path) -> Path:
    p = Path(path)
    payload = {
        "start_ts": result.start_ts,
        "end_ts": result.end_ts,
        "step_s": result.step_s,
        "combined": result.combined_metrics.as_dict(),
        "strategies": {
            sid.value: {
                "starting_equity": r.starting_equity,
                "ending_equity": r.ending_equity,
                "metrics": r.metrics.as_dict(),
                "rejected": r.rejected,
                "extra": r.extra,
                "trades": [
                    {
                        "mint": t.mint, "symbol": t.symbol,
                        "entry": t.entry_price, "exit": t.exit_price,
                        "pnl_usd": round(t.pnl_usd, 4),
                        "pnl_pct": round(t.pnl_pct, 6),
                        "reason": t.exit_reason.value,
                        "entry_ts": t.entry_ts, "exit_ts": t.exit_ts,
                        "wallet": t.wallet,
                    } for t in r.closed
                ],
            } for sid, r in result.results.items()
        },
        "fmea": [
            {"bot": f.bot.value, "name": f.name, "severity": f.severity,
             "likelihood": f.likelihood, "detectability": f.detectability,
             "rpn": f.rpn, "mitigation": f.mitigation, "test": f.test}
            for f in fmea_table()
        ],
    }
    p.write_text(json.dumps(payload, indent=2))
    return p


def write_trades_csv(result: BacktestResult, path: str | Path) -> Path:
    p = Path(path)
    with open(p, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["strategy", "mint", "symbol", "entry_ts", "exit_ts",
                    "entry_price", "exit_price", "qty", "pnl_usd", "pnl_pct",
                    "fees_usd", "exit_reason", "wallet"])
        for sid, r in result.results.items():
            for t in r.closed:
                w.writerow([sid.value, t.mint, t.symbol, t.entry_ts, t.exit_ts,
                            t.entry_price, t.exit_price, t.qty,
                            round(t.pnl_usd, 6), round(t.pnl_pct, 6),
                            round(t.fees_usd, 6), t.exit_reason.value,
                            t.wallet or ""])
    return p


def write_report(result: BacktestResult, out_dir: str | Path) -> dict[str, Path]:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    html = d / "report.html"
    html.write_text(render_html(result))
    return {
        "html": html,
        "json": write_json(result, d / "results.json"),
        "trades": write_trades_csv(result, d / "trades.csv"),
    }
