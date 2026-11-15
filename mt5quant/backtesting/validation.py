"""Strategy validation pipeline - the gate between research and trading.

For one strategy on one or more symbols:

 1. DEVELOPMENT period (first 80% of history): walk-forward optimisation.
    Only the stitched out-of-sample trades are scored.
 2. HOLDOUT period (last 20%): never used for any choice; traded once with
    the parameters the walk-forward ended on.
 3. Monte Carlo on the out-of-sample trades (drawdown / loss probability).
 4. Parameter sensitivity around the final parameters.
 5. Cost stress: the same out-of-sample runs with wider spreads and more
    slippage.
 6. Deflated Sharpe ratio, penalising the number of parameter sets tried.

A strategy is VALIDATED only if every gate passes. A strategy that shines
in-sample and fades out-of-sample is rejected by the walk-forward-efficiency
and holdout gates - that is their purpose. Results obtained on synthetic
data can never produce a VALIDATED status, whatever the numbers say.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from ..ai_engine.meta_model import MetaModel
from ..market_data.timeframes import MarketData
from ..strategies.registry import STRATEGIES, create
from . import metrics as M
from .costs import CostModel
from .engine import Backtester, BacktestResult, BacktestSettings
from .montecarlo import simulate
from .walkforward import sensitivity, walk_forward

VALIDATED, REJECTED, SYNTHETIC_ONLY = "validated", "rejected", "synthetic_only"


@dataclass(frozen=True)
class Gates:
    min_oos_trades: int = 100
    min_holdout_trades: int = 20
    min_expectancy_r: float = 0.05
    min_t_stat: float = 2.0
    min_profit_factor: float = 1.15
    min_sharpe: float = 0.7
    max_drawdown_pct: float = 5.0
    min_wf_efficiency: float = 0.4
    min_profitable_windows: float = 0.6
    min_holdout_expectancy_r: float = 0.0
    max_mc_prob_dd_limit: float = 0.05
    max_mc_prob_loss: float = 0.20
    min_sensitivity_profitable: float = 0.7
    min_stress_expectancy_r: float = 0.0
    min_deflated_sharpe: float = 0.80
    stress_spread_mult: float = 1.5
    stress_slippage_mult: float = 2.0


@dataclass
class ValidationReport:
    strategy: str
    symbols: list[str]
    status: str
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    params: dict = field(default_factory=dict)
    oos: dict = field(default_factory=dict)
    holdout: dict = field(default_factory=dict)
    walk_forward: dict = field(default_factory=dict)
    monte_carlo: dict = field(default_factory=dict)
    sensitivity: dict = field(default_factory=dict)
    stress: dict = field(default_factory=dict)
    baseline: dict = field(default_factory=dict)       # consumed by the live performance monitor
    meta_model: dict = field(default_factory=dict)
    data: dict = field(default_factory=dict)
    synthetic_data: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _combined(trades, equity, initial, synthetic, bars_total=0, bars_in=0) -> BacktestResult:
    return BacktestResult(trades=list(trades), equity=equity, initial_balance=initial,
                          start=equity.index[0] if len(equity) else None,
                          end=equity.index[-1] if len(equity) else None, synthetic=synthetic,
                          bars_total=bars_total, bars_in_market=bars_in)


def validate(data: dict[str, MarketData], base_tf: str, strategy_name: str, cost: CostModel,
             settings: Optional[BacktestSettings] = None, gates: Gates = Gates(), train_days: int = 120,
             test_days: int = 40, holdout_fraction: float = 0.2, models_dir: Optional[str] = None,
             progress: Optional[Callable[[str], None]] = None) -> ValidationReport:
    settings = settings or BacktestSettings()
    say = progress or (lambda m: None)
    frames = [md.frame(base_tf) for md in data.values()]
    first, last = min(f.index[0] for f in frames), max(f.index[-1] for f in frames)
    split = first + (last - first) * (1.0 - holdout_fraction)
    synthetic = any(f.attrs.get("synthetic") for f in frames)
    rep = ValidationReport(strategy_name, sorted(data), REJECTED, synthetic_data=synthetic)
    rep.data = {"start": str(first), "end": str(last), "holdout_from": str(split), "base_tf": base_tf,
                "bars": int(sum(len(f) for f in frames)), "synthetic": synthetic}

    # 1. walk-forward on the development period ---------------------------------
    say(f"{strategy_name}: walk-forward ({train_days}d train / {test_days}d test)")
    wf = walk_forward(data, base_tf, strategy_name, cost, settings, start=first, end=split,
                      train_days=train_days, test_days=test_days, progress=progress)
    rep.walk_forward = wf.summary()
    params = {**STRATEGIES[strategy_name].defaults, **wf.last_params}
    rep.params = params
    oos = _combined(wf.oos_trades, wf.oos_equity, settings.initial_balance, synthetic)
    n_trials = max(wf.combos_tested, 1)
    rep.oos = M.compute(oos, n_trials=n_trials)

    # 2. untouched holdout --------------------------------------------------------
    say(f"{strategy_name}: holdout test from {split:%Y-%m-%d}")
    final_bt = Backtester(data, base_tf, [create(strategy_name, params)], cost, settings)
    holdout = final_bt.run(split, None)
    rep.holdout = M.compute(holdout)

    # 3. Monte Carlo ---------------------------------------------------------------
    r_oos = oos.r_multiples
    rep.monte_carlo = simulate(r_oos, risk_pct=settings.limits.risk_per_trade_pct, block=3,
                               dd_limit_pct=settings.limits.max_drawdown_pct)

    # 4. sensitivity ---------------------------------------------------------------
    say(f"{strategy_name}: parameter sensitivity")
    sens = sensitivity(data, base_tf, strategy_name, params, cost, settings, start=first, end=split)
    rep.sensitivity = {k: v for k, v in sens.items() if k != "table"}
    rep.sensitivity["table"] = sens["table"]

    # 5. cost stress ----------------------------------------------------------------
    say(f"{strategy_name}: cost stress test")
    stressed = cost.stressed(gates.stress_spread_mult, gates.stress_slippage_mult)
    stress_bt = Backtester(data, base_tf, [create(strategy_name, params)], stressed, settings)
    stress_res = stress_bt.run(first + pd.Timedelta(days=train_days), None)
    sr = stress_res.r_multiples
    rep.stress = {"spread_mult": gates.stress_spread_mult, "slippage_mult": gates.stress_slippage_mult,
                  "trades": int(len(sr)), "expectancy_r": float(sr.mean()) if len(sr) else 0.0}

    # ---- gates ---------------------------------------------------------------------
    o, h, mc = rep.oos, rep.holdout, rep.monte_carlo

    def gate(ok: bool, text: str) -> None:
        (rep.passed if ok else rep.failed).append(text)

    gate(o["trades"] >= gates.min_oos_trades, f"out-of-sample trades {o['trades']} (min {gates.min_oos_trades})")
    gate(o["expectancy_r"] >= gates.min_expectancy_r,
         f"OOS expectancy {o['expectancy_r']:+.3f}R (min {gates.min_expectancy_r:+.2f}R)")
    gate(o["t_stat_r"] >= gates.min_t_stat, f"OOS t-statistic {o['t_stat_r']:.2f} (min {gates.min_t_stat})")
    gate(o["profit_factor"] >= gates.min_profit_factor,
         f"OOS profit factor {o['profit_factor']:.2f} (min {gates.min_profit_factor})")
    gate(o["sharpe"] >= gates.min_sharpe, f"OOS Sharpe {o['sharpe']:.2f} (min {gates.min_sharpe})")
    gate(o["max_drawdown_pct"] <= gates.max_drawdown_pct and not oos.flags,
         f"OOS max drawdown {o['max_drawdown_pct']:.2f}% (max {gates.max_drawdown_pct}%)")
    gate(wf.efficiency >= gates.min_wf_efficiency,
         f"walk-forward efficiency {wf.efficiency:.2f} (min {gates.min_wf_efficiency})")
    gate(wf.profitable_windows >= gates.min_profitable_windows,
         f"profitable OOS windows {wf.profitable_windows:.0%} (min {gates.min_profitable_windows:.0%})")
    gate(h["trades"] >= gates.min_holdout_trades and h["expectancy_r"] > gates.min_holdout_expectancy_r,
         f"holdout: {h['trades']} trades, expectancy {h['expectancy_r']:+.3f}R "
         f"(min {gates.min_holdout_trades} trades, > {gates.min_holdout_expectancy_r:+.2f}R)")
    gate(not any("max drawdown" in f for f in holdout.flags), "holdout did not hit the drawdown suspension")
    if mc.get("valid"):
        gate(mc["prob_dd_limit"] <= gates.max_mc_prob_dd_limit,
             f"Monte Carlo P(drawdown >= limit) {mc['prob_dd_limit']:.1%} (max {gates.max_mc_prob_dd_limit:.0%})")
        gate(mc["prob_loss"] <= gates.max_mc_prob_loss,
             f"Monte Carlo P(losing) {mc['prob_loss']:.1%} (max {gates.max_mc_prob_loss:.0%})")
    else:
        gate(False, "Monte Carlo not possible (too few trades)")
    gate(rep.sensitivity["profitable_fraction"] >= gates.min_sensitivity_profitable,
         f"parameter neighbours profitable {rep.sensitivity['profitable_fraction']:.0%} "
         f"(min {gates.min_sensitivity_profitable:.0%})")
    gate(rep.stress["expectancy_r"] > gates.min_stress_expectancy_r,
         f"cost-stress expectancy {rep.stress['expectancy_r']:+.3f}R (must stay > 0)")
    gate(o["deflated_sharpe"] >= gates.min_deflated_sharpe,
         f"deflated Sharpe probability {o['deflated_sharpe']:.2f} over {n_trials} trials "
         f"(min {gates.min_deflated_sharpe})")

    if rep.failed:
        rep.status = REJECTED
    elif synthetic:
        rep.status = SYNTHETIC_ONLY
        rep.failed.append("data is SYNTHETIC: statistical gates passed, but this proves nothing about real "
                          "markets - re-run on broker history")
    else:
        rep.status = VALIDATED
    rep.baseline = {"expectancy_r": o["expectancy_r"], "win_rate": o["win_rate"],
                    "profit_factor": o["profit_factor"], "mc_dd95_r": mc.get("dd95_r")}

    # 6. meta-model (only worth training for a strategy that survived) -------------
    if rep.status in (VALIDATED, SYNTHETIC_ONLY) and models_dir:
        say(f"{strategy_name}: training meta-model on development trades")
        dev = final_bt.run(first, split)
        ids = [int(t.client_id) for t in dev.trades if t.risk_amount > 0]
        if ids:
            X = final_bt.features_for(ids)
            r = np.array([t.r_multiple for t in dev.trades if t.risk_amount > 0])
            entry = np.array([np.datetime64(t.time_open.replace(tzinfo=None)) for t in dev.trades
                              if t.risk_amount > 0])
            exit_ = np.array([np.datetime64(t.time_close.replace(tzinfo=None)) for t in dev.trades
                              if t.risk_amount > 0])
            model = MetaModel(strategy_name)
            mrep = model.fit(X, r, entry, exit_)
            rep.meta_model = {"validated": mrep.validated, "oos_auc": mrep.oos_auc, "samples": mrep.samples,
                              "reasons": mrep.reasons}
            if model.model is not None:
                model.save(models_dir)
    return rep
