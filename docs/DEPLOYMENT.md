# Deployment

## Platform facts

- The MetaTrader 5 terminal and the official `MetaTrader5` Python package run
  on **Windows**. There is no supported native Linux/macOS build of the package.
- On macOS or Linux you can develop, backtest, validate and paper-trade (on the
  synthetic feed or through the bridge), but the terminal must live on a
  Windows desktop or Windows VPS.

## Topology A - everything on one Windows machine / VPS (simplest)

```
[ Windows ]  MT5 terminal  <--IPC-->  python main.py run  (engine + dashboard)
```

1. Install MetaTrader 5, log in to your **demo** account, enable *Algo Trading*.
2. Install Python 3.10+ and create a virtual environment:
   ```
   py -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill `MT5_LOGIN`, `MT5_PASSWORD`,
   `MT5_SERVER` (or leave them empty to use the account already logged in).
4. Create `config/local.yaml`:
   ```yaml
   mode: demo
   broker_backend: mt5
   symbols: [EURUSD, GBPUSD]     # exact names from your Market Watch
   ```
5. `python main.py check`, then `python main.py run`.
6. Autostart: `powershell -ExecutionPolicy Bypass -File deploy\windows\install_tasks.ps1 -Role engine`

## Topology B - engine on Linux/macOS, terminal on a Windows VPS (bridge)

```
[ Linux/macOS ] engine + dashboard  --HMAC/HTTP over tunnel-->  [ Windows VPS ] gateway + MT5 terminal
```

On the Windows VPS:

1. Steps 1-3 above, plus a long random `MT5_BRIDGE_SECRET` in `.env`
   (`python -c "import secrets;print(secrets.token_hex(32))"`).
2. `config/local.yaml`: `mode: demo` (the gateway enforces the account type).
3. `python main.py bridge` (autostart: `install_tasks.ps1 -Role gateway`).

The gateway binds to `127.0.0.1:8787`. **Do not expose it to the internet.**
Reach it through an SSH tunnel, WireGuard or Tailscale:

```
ssh -N -L 8787:127.0.0.1:8787 user@your-vps
```

On the engine machine:

1. Same `MT5_BRIDGE_SECRET` in `.env`, `MT5_BRIDGE_URL=http://127.0.0.1:8787`.
2. `config/local.yaml`: `mode: demo`, `broker_backend: bridge`, your symbols.
3. `python main.py run` (autostart: `deploy/mt5quant.service`).

HMAC gives authenticity, integrity and replay protection; the tunnel gives
confidentiality. Keep both clocks synchronised (NTP): requests older than
`bridge.max_clock_skew_seconds` are rejected.

## Secrets

- Only in `.env` or the process environment. `.env`, `config/local.yaml` and
  `config/live_authorization.json` are git-ignored.
- Logs pass through a redaction filter (known secret values and
  `password=`/`token=`/`api_key=` patterns); account numbers are masked.
- Run `python main.py check` to see which secrets are set (names only).

## Dashboard

- `http://127.0.0.1:8050` by default.
- Set `DASHBOARD_TOKEN` to require a token for every API call and to enable
  **Resume** from the browser. Binding to a non-loopback address without a token
  is refused.
- For remote access use an SSH tunnel or a TLS reverse proxy; the app sends a
  strict Content-Security-Policy and loads nothing from third parties.

## Monitoring and recovery

- Heartbeat: written to the database every cycle (`python main.py status`).
- Watchdog: `scripts/watchdog.py` (cron / Task Scheduler, every minute) alerts
  through the configured notification channels when the heartbeat is stale.
- Connection loss: the engine reconnects with exponential back-off; positions
  stay protected by their broker-side stop-loss and take-profit.
- Restart: safe at any time. State (risk anchors, kill switch, order
  reservations, last evaluated bar) is reloaded from the database and open
  orders are reconciled against the broker before anything new is sent.
- Daily report: written to `reports/` and sent as a notification at
  `engine.daily_report_hour_utc`.

## Notifications

Set any of these in `.env`: `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`,
`NOTIFY_WEBHOOK_URL` (Slack/Discord compatible), or the `SMTP_*` variables.
Critical events (emergency stop, drawdown suspension, abnormal behaviour,
engine down) are never rate-limited.

## Economic calendar

`news.providers`:

- `faireconomy` - Forex Factory's public weekly export (no key).
- `finnhub` - needs `FINNHUB_API_KEY`.
- `file` - your own CSV/JSON (`news.calendar_file`), also usable in backtests.

If no provider can be reached and stored data is older than
`news.stale_after_hours`, new entries are suspended (`news.fail_closed`).
Operator blackouts for unscheduled risk (elections, summits, crises):

```yaml
news:
  manual_blackouts:
    - {start: "2026-11-03T12:00:00Z", end: "2026-11-04T12:00:00Z", currencies: [USD], reason: "election"}
```
