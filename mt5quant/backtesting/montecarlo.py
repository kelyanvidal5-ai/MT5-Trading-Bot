"""Monte Carlo analysis of a trade sequence.

A backtest shows ONE ordering of trades. Resampling the realised
R-multiples (with replacement, optionally in blocks to keep streaks) shows
how much of the result was luck of the sequence, and gives a distribution
for drawdown and final return instead of a single number.
"""
from __future__ import annotations

import numpy as np


def simulate(r_multiples: np.ndarray, risk_pct: float = 0.25, n_sims: int = 5000, n_trades: int | None = None,
             block: int = 1, dd_limit_pct: float = 5.0, skip_prob: float = 0.0, seed: int = 7) -> dict:
    """Bootstrap equity paths with fixed-fractional compounding.

    ``block`` > 1 uses a circular block bootstrap (preserves clustering of
    wins/losses). ``skip_prob`` randomly drops trades (missed fills)."""
    r = np.asarray(r_multiples, dtype=float)
    if len(r) < 10:
        return {"n_trades": int(len(r)), "valid": False}
    rng = np.random.default_rng(seed)
    n = n_trades or len(r)
    if block <= 1:
        idx = rng.integers(0, len(r), size=(n_sims, n))
    else:
        n_blocks = int(np.ceil(n / block))
        starts = rng.integers(0, len(r), size=(n_sims, n_blocks))
        idx = ((starts[:, :, None] + np.arange(block)[None, None, :]) % len(r)).reshape(n_sims, -1)[:, :n]
    sample = r[idx]
    if skip_prob > 0:
        sample = np.where(rng.random(sample.shape) < skip_prob, 0.0, sample)
    growth = np.cumprod(1.0 + sample * risk_pct / 100.0, axis=1)
    growth = np.concatenate([np.ones((n_sims, 1)), growth], axis=1)
    peak = np.maximum.accumulate(growth, axis=1)
    max_dd = ((peak - growth) / peak).max(axis=1) * 100.0
    final = (growth[:, -1] - 1.0) * 100.0
    # drawdown in R units (for the live performance monitor)
    cum_r = np.concatenate([np.zeros((n_sims, 1)), np.cumsum(sample, axis=1)], axis=1)
    dd_r = (np.maximum.accumulate(cum_r, axis=1) - cum_r).max(axis=1)
    return {
        "valid": True, "n_trades": int(n), "n_sims": int(n_sims), "block": int(block),
        "return_p05": float(np.percentile(final, 5)), "return_p50": float(np.percentile(final, 50)),
        "return_p95": float(np.percentile(final, 95)),
        "dd_p50": float(np.percentile(max_dd, 50)), "dd_p95": float(np.percentile(max_dd, 95)),
        "dd_p99": float(np.percentile(max_dd, 99)),
        "dd95_r": float(np.percentile(dd_r, 95)),
        "prob_loss": float((final < 0).mean()),
        "prob_dd_limit": float((max_dd >= dd_limit_pct).mean()),
    }
