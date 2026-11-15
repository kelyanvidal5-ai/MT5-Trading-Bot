import math

import numpy as np
import pandas as pd
import pytest

from mt5quant.backtesting import metrics as M
from mt5quant.backtesting.costs import CostModel
from mt5quant.backtesting.engine import Backtester, BacktestResult, BacktestSettings
from mt5quant.backtesting.montecarlo import simulate
from mt5quant.backtesting.report import backtest_markdown, validation_markdown
from mt5quant.backtesting.validation import REJECTED, SYNTHETIC_ONLY, VALIDATED, Gates, validate
from mt5quant.backtesting.walkforward import grid_combinations, perturbations, sensitivity, walk_forward
from mt5quant.core.models import ClosedTrade, Side
from mt5quant.market_data.specs import generic_spec
from mt5quant.market_data.synthetic import generate_bars
from mt5quant.market_data.timeframes import MarketData, tf_delta
from mt5quant.risk_management.limits import RiskLimits
from mt5quant.strategies.registry import create

COST = CostModel(spread_points={"EURUSD": 8})


@pytest.fixture(scope="module")
def data(market_data):
    return {"EURUSD": market_data}


@pytest.fixture(scope="module")
def result(data):
    bt = Backtester(data, "M5", [create("liquidity_sweep"), create("donchian_breakout")], COST)
    return bt, bt.run()


def test_backtest_produces_trades_with_stops_and_bounded_risk(result):
    bt, res = result
    assert len(res.trades) > 30 and res.signals_taken == len(res.trades)
    assert res.synthetic is True
    equity = res.equity
    for t in res.trades:
        assert t.sl > 0 and t.tp > 0 and t.risk_amount > 0
        assert (t.price_open - t.sl) * t.side.sign > 0
        eq_then = float(equity[equity.index <= pd.Timestamp(t.time_open)].iloc[-1])
        assert t.risk_amount <= eq_then * 0.0025 * 1.005          # within budget even after slippage
        assert t.exit_reason in {"sl", "tp", "time_exit", "end_of_test", "max_drawdown"}
    assert {t.strategy for t in res.trades} == {"liquidity_sweep", "donchian_breakout"}
    assert equity.iloc[-1] == pytest.approx(10_000 + sum(t.profit for t in res.trades), abs=0.05)


def test_signals_execute_at_the_next_bar_open_never_earlier(result, market_data):
    bt, res = result
    events = bt.signal_events()
    m5 = market_data.frame("M5")
    for t in res.trades[:60]:
        ev = events.iloc[int(t.client_id)]
        opened = pd.Timestamp(t.time_open)
        assert opened >= ev["exec_time"]                           # after the signal bar closed
        assert opened - ev["exec_time"] <= 2 * tf_delta("M5")
        bar = m5.loc[opened]
        # filled at that bar's OPEN (+ spread/slippage), not at the signal bar's close
        ref = bar["open"]
        assert abs(t.price_open - ref) <= 0.0006
        if t.side is Side.BUY:
            assert t.price_open >= ref                             # buys pay the ask


def test_backtest_is_deterministic_and_window_runs_are_slices(result):
    bt, res = result
    again = bt.run()
    assert [(t.time_open, t.profit) for t in again.trades] == [(t.time_open, t.profit) for t in res.trades]
    idx = bt.data["EURUSD"].frame("M5").index
    a, b = idx[15_000], idx[30_000]
    part = bt.run(a, b)
    assert part.trades and all(a <= pd.Timestamp(t.time_open) < b + pd.Timedelta(days=5) for t in part.trades)
    assert all(pd.Timestamp(t.time_close) <= b + pd.Timedelta(minutes=6) for t in part.trades)
    assert part.signals_total < res.signals_total


def test_costs_reduce_performance(data):
    strat = [create("liquidity_sweep")]
    free = Backtester(data, "M5", strat, CostModel(spread_points={"EURUSD": 0.0}, default_spread_points=0.0,
                                                   commission_per_lot_per_side=0.0, slippage_points_mean=0.0,
                                                   execution_delay_ms=0, swap_long_points=0.0,
                                                   swap_short_points=0.0)).run()
    costly = Backtester(data, "M5", strat, COST.stressed(2.0, 3.0)).run()
    normal = Backtester(data, "M5", strat, COST).run()
    exp = [float(r.r_multiples.mean()) for r in (free, normal, costly)]
    assert exp[0] > exp[1] > exp[2]
    assert M.compute(normal)["commission_paid"] > 0


def test_selector_and_risk_rejections_are_counted(result):
    _, res = result
    assert res.signals_total == res.signals_taken + sum(res.rejections.values())
    assert any("regime" in k or "trend" in k for k in res.rejections)


def test_drawdown_limit_halts_the_backtest(data):
    """A tighter drawdown limit must suspend trading for the rest of the run."""
    loose = Backtester(data, "M5", [create("liquidity_sweep")], COST).run()
    assert M.drawdown(loose.equity)[0] > 2.5                      # this sample draws down ~3%
    tight = BacktestSettings(limits=RiskLimits(max_drawdown_pct=1.5, max_daily_loss_pct=1.0,
                                               max_weekly_loss_pct=1.5))
    res = Backtester(data, "M5", [create("liquidity_sweep")], COST, tight).run()
    assert any("max drawdown" in f for f in res.flags)
    assert M.drawdown(res.equity)[0] < 2.0                        # stopped right after the breach
    assert len(res.trades) < len(loose.trades)
    assert any("suspended" in k for k in res.rejections) or res.signals_taken < loose.signals_taken


def test_strategy_needs_base_data_fine_enough(market_data):
    from mt5quant.market_data.timeframes import resample
    h1 = MarketData.from_base("EURUSD", generic_spec("EURUSD"), resample(market_data.frame("M5"), "H1"), "H1")
    with pytest.raises(ValueError, match="needs M5 bars"):
        Backtester({"EURUSD": h1}, "H1", [create("scalp_pullback")], COST)


# -------------------------------------------------------------------- metrics
def fake_result(profits, risk=25.0, start="2024-01-01"):
    idx = pd.date_range(start, periods=len(profits) + 1, freq="1D", tz="UTC")
    eq = pd.Series(10_000 + np.concatenate(([0.0], np.cumsum(profits))), index=idx)
    trades = [ClosedTrade(i, "EURUSD", Side.BUY, 0.05, 1.1, 1.1 + p / 5000, idx[i].to_pydatetime(),
                          idx[i + 1].to_pydatetime(), profit=p, sl=1.095, tp=1.11, risk_amount=risk)
              for i, p in enumerate(profits)]
    return BacktestResult(trades, eq, 10_000.0, idx[0], idx[-1], bars_total=len(profits), bars_in_market=3)


def test_metrics_on_known_trades():
    m = M.compute(fake_result([50, -25, 50, -25, -25, 75]))
    assert m["trades"] == 6 and m["net_profit"] == 100
    assert m["win_rate"] == 0.5 and m["profit_factor"] == pytest.approx(175 / 75)
    assert m["expectancy"] == pytest.approx(100 / 6) and m["expectancy_r"] == pytest.approx(100 / 6 / 25)
    assert m["avg_win"] == pytest.approx(175 / 3) and m["avg_loss"] == -25
    assert m["avg_realised_rr"] == pytest.approx(175 / 3 / 25)
    assert m["avg_planned_rr"] == pytest.approx(2.0)
    assert m["max_drawdown"] == pytest.approx(50.0)               # 10075 -> 10025
    assert m["max_drawdown_pct"] == pytest.approx(50 / 10_075 * 100)
    assert m["max_losing_streak"] == 2 and m["return_pct"] == pytest.approx(1.0)
    assert m["sharpe"] > 0 and m["sortino"] > m["sharpe"]


def test_metrics_handle_empty_and_all_losing():
    empty = M.compute(BacktestResult([], pd.Series(dtype=float), 10_000.0, None, None))
    assert empty["trades"] == 0 and empty["sharpe"] == 0 and empty["profit_factor"] == 0
    losing = M.compute(fake_result([-25] * 8))
    assert losing["win_rate"] == 0 and losing["profit_factor"] == 0 and losing["sharpe"] < 0


def test_deflated_sharpe_penalises_many_trials():
    rng = np.random.default_rng(0)
    rets = rng.normal(0.0006, 0.005, 400)
    psr = M.probabilistic_sharpe(rets)
    assert 0.9 < psr <= 1.0
    assert M.deflated_sharpe(rets, 1) == pytest.approx(psr)
    assert M.deflated_sharpe(rets, 50) < M.deflated_sharpe(rets, 5) < psr
    assert M.probabilistic_sharpe(rng.normal(0, 0.005, 400)) < 0.9
    assert abs(M._norm_ppf(0.975) - 1.95996) < 1e-4


# ---------------------------------------------------------------- monte carlo
def test_monte_carlo_distributions():
    rng = np.random.default_rng(1)
    good = np.where(rng.random(300) < 0.5, 1.6, -1.0)
    mc = simulate(good, risk_pct=0.25, n_sims=2000, dd_limit_pct=5.0)
    assert mc["valid"] and mc["return_p05"] < mc["return_p50"] < mc["return_p95"]
    assert mc["dd_p50"] <= mc["dd_p95"] <= mc["dd_p99"] and mc["prob_loss"] < 0.05
    bad = simulate(np.where(rng.random(300) < 0.3, 1.5, -1.0), n_sims=2000)
    assert bad["prob_loss"] > 0.95 and bad["prob_dd_limit"] > mc["prob_dd_limit"]
    assert simulate(good[:5])["valid"] is False
    # more risk per trade -> deeper drawdowns ; block bootstrap and trade skipping run
    assert simulate(good, risk_pct=1.0, n_sims=1000)["dd_p95"] > mc["dd_p95"]
    assert simulate(good, block=5, n_sims=500)["valid"] and simulate(good, skip_prob=0.2, n_sims=500)["valid"]
    assert simulate(good, n_sims=500, seed=3) == simulate(good, n_sims=500, seed=3)


# --------------------------------------------------------------- walk-forward
def test_grid_and_perturbations():
    combos = grid_combinations({"a": [1, 2], "b": [0.5, 1.0, 1.5]})
    assert len(combos) == 6 and {"a": 2, "b": 1.5} in combos
    s = create("donchian_breakout")
    pert = perturbations(s)
    assert all(p["params"][p["param"]] != s.params[p["param"]] for p in pert)
    assert not any(p["param"] == "min_rr" for p in pert)
    ints = [p for p in pert if p["param"] == "channel"]
    assert all(isinstance(p["params"]["channel"], int) for p in ints) and len(ints) == 4


@pytest.fixture(scope="module")
def wf(data):
    return walk_forward(data, "M5", "liquidity_sweep", COST, train_days=60, test_days=30,
                        grid={"min_pierce_atr": [0.05, 0.2], "rr": [1.5, 2.5]})


def test_walk_forward_windows_are_strictly_out_of_sample(wf):
    assert len(wf.windows) >= 4 and wf.combos_tested == 4
    for w in wf.windows:
        assert w.is_start < w.is_end < w.oos_end
        assert (w.is_end - w.is_start).days == 60
    for a, b in zip(wf.windows, wf.windows[1:]):
        assert b.is_end == a.oos_end                               # OOS segments tile, never overlap
    for t in wf.oos_trades:
        assert any(w.is_end <= pd.Timestamp(t.time_open) < w.oos_end + pd.Timedelta(days=4) for w in wf.windows)
    first_oos = wf.windows[0].is_end
    assert all(pd.Timestamp(t.time_open) >= first_oos for t in wf.oos_trades)
    assert wf.oos_equity.index.is_monotonic_increasing
    assert wf.oos_equity.iloc[-1] == pytest.approx(10_000 + sum(t.profit for t in wf.oos_trades), abs=0.5)
    s = wf.summary()
    assert s["oos_trades"] == len(wf.oos_trades) and 0 <= s["profitable_windows"] <= 1


def test_sensitivity_report(data):
    idx = data["EURUSD"].frame("M5").index
    s = sensitivity(data, "M5", "donchian_breakout", {}, COST, start=idx[0], end=idx[25_000])
    assert s["neighbours"] >= 20 and 0 <= s["profitable_fraction"] <= 1
    assert {r["param"] for r in s["table"]} >= {"channel", "sl_atr", "rr"}


# ----------------------------------------------------------------- validation
@pytest.fixture(scope="module")
def noise_data():
    """No drift, no regimes: nothing to exploit. Costs make every strategy lose."""
    bars = generate_bars("EURUSD", periods=45_000, tf="M5", seed=21, drift_strength=0.0)
    return {"EURUSD": MarketData.from_base("EURUSD", generic_spec("EURUSD"), bars, "M5")}


def test_validation_rejects_strategy_on_data_with_no_edge(noise_data, tmp_path):
    rep = validate(noise_data, "M5", "liquidity_sweep", COST, train_days=50, test_days=25,
                   models_dir=str(tmp_path))
    assert rep.status == REJECTED and len(rep.failed) >= 3
    assert rep.synthetic_data and rep.walk_forward["windows"] >= 3
    assert not rep.meta_model and not list(tmp_path.iterdir())     # nothing is trained for a rejected strategy
    assert rep.baseline["expectancy_r"] == rep.oos["expectancy_r"]
    text = validation_markdown([rep])
    assert "REJECTED" in text and "SYNTHETIC DATA" in text and "FAIL:" in text


def test_synthetic_data_can_never_be_validated(data, tmp_path):
    """Even with every statistical gate switched off, synthetic data yields
    SYNTHETIC_ONLY - never VALIDATED."""
    lax = Gates(min_oos_trades=1, min_holdout_trades=0, min_expectancy_r=-9, min_t_stat=-99, min_profit_factor=0,
                min_sharpe=-99, max_drawdown_pct=100, min_wf_efficiency=-9, min_profitable_windows=0,
                min_holdout_expectancy_r=-9, max_mc_prob_dd_limit=1, max_mc_prob_loss=1,
                min_sensitivity_profitable=0, min_stress_expectancy_r=-9, min_deflated_sharpe=0)
    rep = validate(data, "M5", "donchian_breakout", COST, gates=lax, train_days=60, test_days=30,
                   models_dir=str(tmp_path))
    assert rep.status == SYNTHETIC_ONLY != VALIDATED
    assert any("SYNTHETIC" in f for f in rep.failed)
    assert rep.params["channel"] in (20, 30, 40) and rep.meta_model     # meta-model step ran
    assert rep.meta_model["validated"] is False                         # too few trades to accept a model


def test_report_tables():
    m = M.compute(fake_result([50, -25, 50, -25, -25, 75]))
    md = backtest_markdown("Backtest", {"demo": m}, notes=["note"], synthetic=True, data_info={"bars": 6})
    assert "| demo | 6 |" in md and "SYNTHETIC DATA" in md and "- note" in md
    assert not math.isnan(m["sharpe"])
