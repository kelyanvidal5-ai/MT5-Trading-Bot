"""Command-line and watchdog smoke tests (isolated database per test)."""
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def cfg_file(tmp_path):
    base = yaml.safe_load((ROOT / "config" / "default.yaml").read_text())
    base["database_path"] = str(tmp_path / "cli.db")
    base["log_dir"] = str(tmp_path / "logs")
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml.safe_dump(base))
    return p


def run(cfg, *args, script="main.py"):
    cmd = [sys.executable, str(ROOT / script)]
    cmd += (["--config", str(cfg)] if script == "main.py" else []) + list(args)
    if script != "main.py":
        cmd += ["--config", str(cfg)]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=300)


def test_check_reports_platform_truthfully(cfg_file):
    r = run(cfg_file, "check")
    assert r.returncode == 0, r.stderr
    assert "mode=paper" in r.stdout and "LOCKED (default)" in r.stdout and "audit log       : intact" in r.stdout
    if sys.platform != "win32":
        assert "Windows-only" in r.stdout


def test_stop_status_resume_cycle(cfg_file):
    assert run(cfg_file, "stop", "cli test").returncode == 0
    out = run(cfg_file, "status").stdout
    assert "emergency stop=True" in out and "NOT RUNNING" in out
    refused = run(cfg_file, "resume")
    assert refused.returncode == 2 and "Refusing to resume without --confirm" in refused.stdout
    assert run(cfg_file, "resume", "--confirm").returncode == 0
    assert "emergency stop=False" in run(cfg_file, "status").stdout


def test_backtest_command_writes_labelled_report(cfg_file, tmp_path):
    r = run(cfg_file, "backtest", "--synthetic", "20000", "--symbols", "EURUSD", "--strategies",
            "liquidity_sweep", "--out", str(tmp_path / "rep"))
    assert r.returncode == 0, r.stderr
    report = next((tmp_path / "rep").glob("backtest_*.md")).read_text()
    assert "SYNTHETIC DATA" in report and "liquidity_sweep" in report
    assert run(cfg_file, "backtest").returncode != 0                      # no data source given


def test_authorize_live_refuses_without_demo_record(cfg_file):
    r = run(cfg_file, "authorize-live")
    assert r.returncode == 2 and "REFUSED" in r.stdout
    assert not (ROOT / "config" / "live_authorization.json").exists()


def test_bridge_command_fails_clearly_off_windows(cfg_file):
    if sys.platform == "win32":
        pytest.skip("off-Windows behaviour")
    r = run(cfg_file, "bridge")
    assert r.returncode != 0 and "only exists for Windows" in (r.stderr + r.stdout)


def test_watchdog_alerts_when_engine_is_down(cfg_file):
    r = run(cfg_file, script="scripts/watchdog.py")
    assert r.returncode == 1 and "ALERT" in r.stdout
