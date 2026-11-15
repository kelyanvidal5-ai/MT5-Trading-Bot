"""Backtest and validation reports (Markdown + JSON)."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from .validation import ValidationReport

SYNTHETIC_BANNER = ("> **SYNTHETIC DATA.** These numbers come from generated random-walk prices, not from a "
                    "market. They demonstrate that the pipeline works; they say nothing about profitability.\n")


def _f(x, nd=2) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        if math.isinf(x):
            return "inf"
        if math.isnan(x):
            return "n/a"
        return f"{x:.{nd}f}"
    return str(x)


def metrics_table(rows: dict[str, dict]) -> str:
    cols = [("trades", "Trades", 0), ("net_profit", "Net P/L", 2), ("return_pct", "Return %", 2),
            ("profit_factor", "PF", 2), ("win_rate", "Win %", 1), ("expectancy_r", "Exp (R)", 3),
            ("t_stat_r", "t-stat", 2), ("avg_realised_rr", "Avg R:R", 2), ("sharpe", "Sharpe", 2),
            ("sortino", "Sortino", 2), ("max_drawdown_pct", "Max DD %", 2)]
    head = "| Strategy | " + " | ".join(c[1] for c in cols) + " |"
    sep = "|---|" + "|".join("---:" for _ in cols) + "|"
    lines = [head, sep]
    for name, m in rows.items():
        vals = []
        for key, _, nd in cols:
            v = m.get(key)
            if key == "win_rate" and v is not None:
                v = v * 100.0
            vals.append(_f(float(v) if isinstance(v, (int, float)) and nd else v, nd) if nd else str(v))
        lines.append(f"| {name} | " + " | ".join(vals) + " |")
    return "\n".join(lines)


def backtest_markdown(title: str, rows: dict[str, dict], notes: Optional[list[str]] = None,
                      synthetic: bool = False, data_info: Optional[dict] = None) -> str:
    out = [f"# {title}", ""]
    if synthetic:
        out += [SYNTHETIC_BANNER]
    if data_info:
        out += ["**Data:** " + ", ".join(f"{k}={v}" for k, v in data_info.items()), ""]
    out += [metrics_table(rows), ""]
    for n in notes or []:
        out.append(f"- {n}")
    return "\n".join(out) + "\n"


def validation_markdown(reports: list[ValidationReport]) -> str:
    out = ["# Strategy validation report", ""]
    if any(r.synthetic_data for r in reports):
        out += [SYNTHETIC_BANNER]
    out += ["| Strategy | Verdict | OOS trades | OOS exp (R) | t-stat | PF | Sharpe | Max DD % | WF eff. | "
            "Holdout exp (R) | Gates failed |", "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in reports:
        o, h = r.oos, r.holdout
        out.append(f"| {r.strategy} | **{r.status.upper()}** | {o.get('trades', 0)} | "
                   f"{_f(o.get('expectancy_r'), 3)} | {_f(o.get('t_stat_r'))} | {_f(o.get('profit_factor'))} | "
                   f"{_f(o.get('sharpe'))} | {_f(o.get('max_drawdown_pct'))} | "
                   f"{_f(r.walk_forward.get('efficiency'))} | {_f(h.get('expectancy_r'), 3)} | {len(r.failed)} |")
    for r in reports:
        out += ["", f"## {r.strategy} - {r.status.upper()}", "",
                f"Symbols: {', '.join(r.symbols)} | data {r.data.get('start', '')[:10]} to "
                f"{r.data.get('end', '')[:10]} | holdout from {r.data.get('holdout_from', '')[:10]}",
                f"Final parameters: `{json.dumps(r.params, default=str)}`", ""]
        if r.failed:
            out.append("Failed gates:")
            out += [f"- FAIL: {x}" for x in r.failed]
        if r.passed:
            out.append("")
            out.append("Passed gates:")
            out += [f"- pass: {x}" for x in r.passed]
        mc = r.monte_carlo
        if mc.get("valid"):
            out += ["", f"Monte Carlo ({mc['n_sims']} paths of {mc['n_trades']} trades): return p05/p50/p95 = "
                        f"{mc['return_p05']:+.2f}% / {mc['return_p50']:+.2f}% / {mc['return_p95']:+.2f}%, "
                        f"max drawdown p50/p95/p99 = {mc['dd_p50']:.2f}% / {mc['dd_p95']:.2f}% / "
                        f"{mc['dd_p99']:.2f}%"]
        wt = r.walk_forward.get("window_table") or []
        if wt:
            out += ["", "| In-sample | OOS until | IS exp (R) | IS trades | OOS exp (R) | OOS trades | OOS net | "
                        "Params |", "|---|---|---:|---:|---:|---:|---:|---|"]
            for w in wt:
                out.append(f"| {w['is']} | {w['oos_until']} | {w['is_exp_r']:+.3f} | {w['is_trades']} | "
                           f"{w['oos_exp_r']:+.3f} | {w['oos_trades']} | {w['oos_net']:+.2f} | "
                           f"`{json.dumps(w['params'])}` |")
        if r.meta_model:
            mm = r.meta_model
            out += ["", f"Meta-model: {'accepted' if mm.get('validated') else 'rejected'} "
                        f"(OOS AUC {_f(mm.get('oos_auc'), 3)}, {mm.get('samples')} trades)"
                        + ("" if mm.get("validated") else " - " + "; ".join(mm.get("reasons", [])))]
    return "\n".join(out) + "\n"


def save(text: str, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def save_json(obj, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, default=str))
    return p
