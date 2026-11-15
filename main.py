#!/usr/bin/env python3
"""MT5 Quant AI - command line entry point.

    python main.py check                 environment and configuration diagnostics
    python main.py run                   start the autonomous engine (+ dashboard)
    python main.py dashboard             dashboard only
    python main.py backtest ...          backtest strategies
    python main.py validate ...          full validation pipeline (walk-forward, Monte Carlo, ...)
    python main.py research ...          recurring-pattern research report
    python main.py status                engine health and risk state
    python main.py stop "reason"         EMERGENCY STOP (blocks entries, flattens positions)
    python main.py resume --confirm      resume after a stop / suspension
    python main.py bridge                MT5 gateway (run on the Windows machine with the terminal)
    python main.py download-history ...  save broker history to CSV for backtesting
    python main.py authorize-live        interactive authorization of real-money trading

The default mode is PAPER. Nothing in this file can enable live trading on
its own - see docs/LIVE_TRADING.md.
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import platform
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from mt5quant.core.config import LIVE_AUTH_PHRASE, ROOT, Secrets, load_config
from mt5quant.core.logging_setup import mask_login, setup_logging
from mt5quant.core.models import UTC, TradingMode
from mt5quant.database.db import Database

log = logging.getLogger("mt5quant")


def _db(cfg) -> Database:
    return Database(cfg.resolve_path(cfg.database_path))


def _load_data(args, cfg):
    """Returns ({symbol: MarketData}, base_tf, info)."""
    from mt5quant.market_data.loader import load_bars, quality_report
    from mt5quant.market_data.specs import generic_spec
    from mt5quant.market_data.synthetic import generate_bars
    from mt5quant.market_data.timeframes import MarketData

    data, info = {}, {}
    if args.data:
        for item in args.data:
            symbol, _, path = item.partition("=")
            if not path:
                raise SystemExit("--data expects SYMBOL=path.csv")
            bars = load_bars(path)
            data[symbol] = MarketData.from_base(symbol, generic_spec(symbol, cfg.paper.currency), bars, args.base_tf)
            info[symbol] = quality_report(bars, args.base_tf)
    else:
        if not args.synthetic:
            raise SystemExit("provide --data SYMBOL=file.csv (broker history) or --synthetic N (generated bars)")
        for symbol in (args.symbols or cfg.symbols):
            bars = generate_bars(symbol, periods=args.synthetic, tf=args.base_tf, seed=args.seed,
                                 spread_points=cfg.costs.spread_points.get(symbol, cfg.costs.default_spread_points))
            data[symbol] = MarketData.from_base(symbol, generic_spec(symbol, cfg.paper.currency), bars, args.base_tf)
            info[symbol] = quality_report(bars, args.base_tf)
    return data, args.base_tf, info


def _settings(cfg):
    from mt5quant.backtesting.engine import BacktestSettings
    from mt5quant.market_data.sessions import MarketCalendar

    s = cfg.sessions
    return BacktestSettings(initial_balance=cfg.paper.initial_balance, currency=cfg.paper.currency,
                            leverage=cfg.paper.leverage, limits=cfg.risk,
                            min_confidence=cfg.strategies.min_confidence,
                            calendar=MarketCalendar(s.avoid_rollover_minutes, s.friday_cutoff_hours_before_close,
                                                    s.monday_open_delay_minutes, s.holidays),
                            day_reset_hour_utc=cfg.engine.day_reset_hour_utc)


# ------------------------------------------------------------------- commands
def cmd_check(args, cfg) -> int:
    print(f"platform        : {platform.system()} {platform.release()} / Python {platform.python_version()}")
    print(f"config          : valid  (mode={cfg.mode.value}, symbols={cfg.symbols})")
    print(f"risk envelope   : {cfg.risk.risk_per_trade_pct}% per trade, {cfg.risk.max_daily_loss_pct}% daily, "
          f"{cfg.risk.max_drawdown_pct}% max drawdown")
    if sys.platform == "win32":
        try:
            import MetaTrader5  # noqa: F401

            print("MetaTrader5 pkg : installed (direct terminal access possible)")
        except ImportError:
            print("MetaTrader5 pkg : NOT installed -> pip install MetaTrader5")
    else:
        print("MetaTrader5 pkg : unavailable on this OS (the official package is Windows-only).")
        print("                  Use broker_backend: bridge with the gateway on a Windows machine/VPS.")
    for name in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_BRIDGE_SECRET", "DASHBOARD_TOKEN",
                 "TELEGRAM_BOT_TOKEN", "FINNHUB_API_KEY"):
        print(f"secret {name:<19}: {'set' if Secrets.get(name) else 'not set'}")
    live = Secrets.get("MT5QUANT_LIVE_AUTHORIZED") == LIVE_AUTH_PHRASE
    print(f"live trading    : {'env phrase present' if live else 'LOCKED (default)'}")
    db = _db(cfg)
    ok, bad = db.verify_audit_chain()
    print(f"audit log       : {'intact' if ok else f'BROKEN at row {bad}'}")
    return 0


def cmd_run(args, cfg) -> int:
    from mt5quant.core.factory import build_engine

    db = _db(cfg)
    engine = build_engine(cfg, db)
    if not args.no_dashboard:
        from mt5quant.dashboard.app import serve_in_thread

        serve_in_thread(cfg, db)
        log.info("dashboard: http://%s:%d", cfg.dashboard.host, cfg.dashboard.port)
    try:
        engine.run_forever()
    except KeyboardInterrupt:
        log.info("interrupted - stopping (open positions keep their broker-side stops)")
    return 0


def cmd_dashboard(args, cfg) -> int:
    import uvicorn

    from mt5quant.dashboard.app import create_app

    uvicorn.run(create_app(cfg, _db(cfg)), host=cfg.dashboard.host, port=cfg.dashboard.port, log_level="warning")
    return 0


def cmd_backtest(args, cfg) -> int:
    from mt5quant.backtesting import metrics as M
    from mt5quant.backtesting.costs import CostModel
    from mt5quant.backtesting.engine import Backtester
    from mt5quant.backtesting.report import backtest_markdown, save, save_json
    from mt5quant.strategies.registry import create

    data, base_tf, info = _load_data(args, cfg)
    names = args.strategies or cfg.strategies.enabled
    cost = CostModel.from_config(cfg.costs)
    rows = {}
    for name in names:
        res = Backtester(data, base_tf, [create(name, cfg.strategies.params.get(name))], cost, _settings(cfg)).run()
        rows[name] = M.compute(res)
        print(f"{name:<18} trades {rows[name]['trades']:>4}  net {rows[name]['net_profit']:>+9.2f}  "
              f"PF {rows[name]['profit_factor']:.2f}  exp {rows[name]['expectancy_r']:+.3f}R  "
              f"Sharpe {rows[name]['sharpe']:.2f}  maxDD {rows[name]['max_drawdown_pct']:.2f}%")
    if len(names) > 1:
        combo = Backtester(data, base_tf, [create(n, cfg.strategies.params.get(n)) for n in names], cost,
                           _settings(cfg)).run()
        rows["ALL (portfolio)"] = M.compute(combo)
    synthetic = any(i["synthetic"] for i in info.values())
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out = Path(args.out or cfg.resolve_path("reports"))
    md = backtest_markdown("Backtest report", rows, synthetic=synthetic, data_info={
        "symbols": ",".join(data), "base_tf": base_tf, "bars": sum(i["bars"] for i in info.values()),
        "from": min(i["start"] for i in info.values())[:10], "to": max(i["end"] for i in info.values())[:10]},
        notes=["Single-pass backtests are descriptive only. Use `python main.py validate` before trusting any "
               "strategy: it adds walk-forward, holdout, Monte Carlo, sensitivity and cost-stress gates."])
    path = save(md, out / f"backtest_{stamp}.md")
    save_json({"metrics": rows, "data": info}, out / f"backtest_{stamp}.json")
    print(f"\nreport: {path}")
    return 0


def cmd_validate(args, cfg) -> int:
    from mt5quant.backtesting.costs import CostModel
    from mt5quant.backtesting.report import save, save_json, validation_markdown
    from mt5quant.backtesting.validation import Gates, validate
    from mt5quant.core.factory import MODELS_DIR

    data, base_tf, info = _load_data(args, cfg)
    names = args.strategies or cfg.strategies.enabled
    cost = CostModel.from_config(cfg.costs)
    db = _db(cfg)
    reports = []
    gates = Gates(max_drawdown_pct=cfg.risk.max_drawdown_pct)
    for name in names:
        rep = validate(data, base_tf, name, cost, _settings(cfg), gates=gates, train_days=args.train_days,
                       test_days=args.test_days, models_dir=str(MODELS_DIR),
                       progress=(print if args.verbose else None))
        reports.append(rep)
        slim = rep.to_dict()
        slim["sensitivity"].pop("table", None)
        for symbol in data:
            db.set_strategy_status(name, symbol, rep.status, slim)
        db.audit("strategy_validated", {"strategy": name, "status": rep.status, "symbols": list(data),
                                        "failed_gates": len(rep.failed)}, actor="cli")
        print(f"{name:<18} {rep.status.upper():<15} OOS {rep.oos['trades']:>4} trades, "
              f"{rep.oos['expectancy_r']:+.3f}R, PF {rep.oos['profit_factor']:.2f} | "
              f"{len(rep.failed)} gate(s) failed")
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    out = Path(args.out or cfg.resolve_path("reports"))
    path = save(validation_markdown(reports), out / f"validation_{stamp}.md")
    save_json([r.to_dict() for r in reports], out / f"validation_{stamp}.json")
    print(f"\nreport: {path}")
    return 0


def cmd_research(args, cfg) -> int:
    from mt5quant.ai_engine.patterns import mine

    data, base_tf, _ = _load_data(args, cfg)
    for symbol, md in data.items():
        tf = args.timeframe if md.has(args.timeframe) else base_tf
        res = mine(md.frame(tf), horizon=args.horizon)
        print(f"\n{symbol} {tf}: {len(res)} patterns tested, "
              f"{int(res['significant_after_fdr'].sum()) if len(res) else 0} significant after FDR control")
        if len(res):
            print(res.head(12).to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    print("\nResearch output only - nothing here is traded without passing `validate`.")
    return 0


def cmd_status(args, cfg) -> int:
    from mt5quant.monitoring import reports, stats
    from mt5quant.risk_management.manager import RiskManager

    db = _db(cfg)
    h = reports.health(db)
    print(f"engine   : {'ALIVE' if h['alive'] else 'NOT RUNNING'} ({h['status']}, heartbeat age "
          f"{h['age_seconds'] if h['age_seconds'] is None else round(h['age_seconds'])}s)")
    risk = RiskManager(cfg.risk, db).snapshot()
    print(f"risk     : daily loss {risk['daily_loss_pct']:.2f}% | weekly {risk['weekly_loss_pct']:.2f}% | "
          f"drawdown {risk['drawdown_pct']:.2f}% | suspended={risk['suspended']} | "
          f"emergency stop={risk['kill_switch'].get('active')}")
    s = stats.summary(db, cfg.mode.value)
    print(f"trades   : {s['trades']} total | win rate {s['win_rate']:.0%} | net {s['net_profit']:+.2f}")
    for st in db.strategy_statuses():
        print(f"strategy : {st['strategy']:<18} {st['symbol']:<10} {st['status']}")
    return 0


def cmd_stop(args, cfg) -> int:
    from mt5quant.risk_management.manager import RiskManager

    RiskManager(cfg.risk, _db(cfg)).emergency_stop(args.reason or "manual stop from CLI", actor="cli")
    print("EMERGENCY STOP set. The engine blocks new entries and flattens positions on its next cycle.")
    return 0


def cmd_resume(args, cfg) -> int:
    from mt5quant.risk_management.manager import RiskManager

    if not args.confirm:
        print("Refusing to resume without --confirm. Review why trading stopped first (python main.py status).")
        return 2
    db = _db(cfg)
    RiskManager(cfg.risk, db).resume(actor="cli")
    if args.strategy:
        for st in db.strategy_statuses():
            if st["strategy"] == args.strategy and st["status"] == "suspended":
                db.set_strategy_status(st["strategy"], st["symbol"], "untested", {"note": "manually un-suspended"})
        db.audit("strategy_unsuspended", {"strategy": args.strategy}, actor="cli")
        print(f"strategy {args.strategy} un-suspended (status 'untested': re-validate before demo/live use)")
    print("Trading resumed. The drawdown high-water mark restarts from current equity.")
    return 0


def cmd_bridge(args, cfg) -> int:
    import uvicorn

    from mt5quant.bridge.server import create_gateway_app
    from mt5quant.execution.live_gate import LiveGate
    from mt5quant.execution.mt5_broker import MT5Broker

    if cfg.mode not in (TradingMode.DEMO, TradingMode.LIVE, TradingMode.PAPER):
        raise SystemExit("bridge needs mode paper (read-only), demo or live")
    gate = LiveGate(cfg, db=None, require_track_record=False)
    broker = MT5Broker(mode=cfg.mode, gate=gate, magic=cfg.execution.magic)
    if not broker.connect():
        raise SystemExit("could not initialize the MetaTrader 5 terminal")
    acct = broker.account_info()
    log.info("gateway serving account %s (%s) in %s mode on %s:%d", mask_login(acct.login),
             "demo" if acct.is_demo else "REAL", cfg.mode.value, cfg.bridge.host, cfg.bridge.port)
    app = create_gateway_app(broker, Secrets.require("MT5_BRIDGE_SECRET"), cfg.bridge.max_clock_skew_seconds)
    uvicorn.run(app, host=cfg.bridge.host, port=cfg.bridge.port, log_level="warning")
    return 0


def cmd_download(args, cfg) -> int:
    from mt5quant.core.factory import _bridge_broker, _mt5_broker
    from mt5quant.market_data.loader import quality_report, save_bars

    paper_cfg = cfg.model_copy(update={"mode": TradingMode.PAPER})
    broker = _mt5_broker(paper_cfg, None, read_only=True) if args.source == "mt5" else _bridge_broker(cfg)
    if not broker.connect():
        raise SystemExit("could not connect")
    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    out = Path(args.out or cfg.resolve_path("data"))
    for symbol in (args.symbols or cfg.symbols):
        if hasattr(broker, "rates_range"):
            bars = broker.rates_range(symbol, args.timeframe, start, end)
        else:
            bars = broker.rates(symbol, args.timeframe, min(args.days * 1440 // 5, 500_000))
        path = out / f"{symbol}_{args.timeframe}.csv"
        save_bars(bars, path)
        print(f"{symbol}: {json.dumps(quality_report(bars, args.timeframe))} -> {path}")
    return 0


def cmd_authorize_live(args, cfg) -> int:
    from mt5quant.execution.live_gate import LiveGate, write_authorization

    db = _db(cfg)
    rec = db.demo_track_record()
    gate = cfg.demo_gate
    print("=== Authorization of REAL-MONEY trading ===")
    print(f"Demo track record in the database: {rec['trades']} trades over {rec['days']:.0f} days, "
          f"net {rec['net_profit']:+.2f}")
    if rec["trades"] < gate.min_demo_trades or rec["days"] < gate.min_demo_days:
        print(f"REFUSED: at least {gate.min_demo_trades} demo trades over {gate.min_demo_days} days are required.")
        return 2
    if not sys.stdin.isatty():
        print("REFUSED: this command must be run interactively by a person.")
        return 2
    login = input("Account login (number) to authorize: ").strip()
    operator = input("Your name: ").strip() or getpass.getuser()
    typed = input(f"Type exactly {LIVE_AUTH_PHRASE} to confirm: ").strip()
    if typed != LIVE_AUTH_PHRASE or not login.isdigit():
        print("Not confirmed. Nothing was changed.")
        return 2
    path = write_authorization(login, operator)
    db.audit("live_authorization_written", {"account": mask_login(login), "operator": operator}, actor="cli")
    print(f"Authorization written to {path} (valid {gate.authorization_valid_days} days).")
    print("Live trading additionally requires `mode: live` in config/local.yaml and the environment variable "
          "MT5QUANT_LIVE_AUTHORIZED. Remaining blocks right now:")
    for r in LiveGate(cfg, db).check(None)[1]:
        print(f"  - {r}")
    return 0


# ------------------------------------------------------------------------ cli
def _data_args(p) -> None:
    p.add_argument("--data", nargs="*", metavar="SYMBOL=FILE", help="broker history (CSV / MT5 export)")
    p.add_argument("--synthetic", type=int, default=0, metavar="N", help="generate N synthetic bars instead")
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--base-tf", default="M5")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--strategies", nargs="*")
    p.add_argument("--out")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="mt5quant", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", help="alternative YAML config file")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    run = sub.add_parser("run")
    run.add_argument("--no-dashboard", action="store_true")
    sub.add_parser("dashboard")
    bt = sub.add_parser("backtest")
    _data_args(bt)
    va = sub.add_parser("validate")
    _data_args(va)
    va.add_argument("--train-days", type=int, default=120)
    va.add_argument("--test-days", type=int, default=40)
    va.add_argument("--verbose", action="store_true")
    rs = sub.add_parser("research")
    _data_args(rs)
    rs.add_argument("--timeframe", default="H1")
    rs.add_argument("--horizon", type=int, default=4)
    sub.add_parser("status")
    st = sub.add_parser("stop")
    st.add_argument("reason", nargs="?")
    re_ = sub.add_parser("resume")
    re_.add_argument("--confirm", action="store_true")
    re_.add_argument("--strategy")
    sub.add_parser("bridge")
    dl = sub.add_parser("download-history")
    dl.add_argument("--source", choices=["mt5", "bridge"], default="mt5")
    dl.add_argument("--symbols", nargs="*")
    dl.add_argument("--timeframe", default="M5")
    dl.add_argument("--days", type=int, default=730)
    dl.add_argument("--out")
    sub.add_parser("authorize-live")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    quiet = args.cmd in ("check", "status", "stop", "resume", "authorize-live", "backtest", "validate", "research")
    setup_logging(cfg.resolve_path(cfg.log_dir), "WARNING" if quiet else cfg.log_level)
    handlers = {"check": cmd_check, "run": cmd_run, "dashboard": cmd_dashboard, "backtest": cmd_backtest,
                "validate": cmd_validate, "research": cmd_research, "status": cmd_status, "stop": cmd_stop,
                "resume": cmd_resume, "bridge": cmd_bridge, "download-history": cmd_download,
                "authorize-live": cmd_authorize_live}
    return handlers[args.cmd](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
