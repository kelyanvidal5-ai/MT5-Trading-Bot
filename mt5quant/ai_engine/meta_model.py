"""Meta-labelling model: estimates the probability that a strategy signal
ends profitably, from features known at signal time.

Safeguards against leakage and overfitting
------------------------------------------
* Features come from the causal context/indicator pipeline only.
* Labels (trade outcomes) span time, so cross-validation is *purged walk-
  forward*: a fold trains only on trades that had fully CLOSED before the
  test block starts, minus an embargo. No shuffling, ever.
* The model is accepted (``validated``) only if its out-of-sample AUC and
  the realised expectancy of the trades it would keep both beat the
  unfiltered baseline across folds. Otherwise it is stored as "rejected"
  and the engine ignores it.
* A validated model can only blend into confidence or veto a trade. It has
  no access to risk limits, position sizing rules or strategy deployment.
* Model files are verified against a stored SHA-256 before being unpickled.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

FEATURES = ["side", "strength", "regime_dir", "adx_h1", "er_h1", "vol_rank", "shock_z", "trend_H1",
            "trend_H4", "trend_D1", "primary_align", "risk_atr", "reward_risk", "hour_sin", "hour_cos",
            "dow", "atr_pct"]

MIN_SAMPLES = 300
MIN_AUC = 0.55
MIN_FOLD_WIN_FRACTION = 0.6
KEEP_THRESHOLD = 0.5


def build_features(signals: pd.DataFrame, ctx: pd.DataFrame) -> pd.DataFrame:
    """Feature rows for the bars of ``signals`` that carry a signal. Every
    column is known at the close of the signal bar."""
    live = signals["side"].values != 0
    s, c = signals[live], ctx[live]
    side = s["side"].astype(float)
    close, atr_ = s["close"], s["atr"]
    hours = s.index.hour + s.index.minute / 60.0
    f = pd.DataFrame(index=s.index)
    f["side"] = side
    f["strength"] = s["strength"]
    for col in ("regime_dir", "adx_h1", "er_h1", "vol_rank", "shock_z", "trend_H1", "trend_H4", "trend_D1"):
        f[col] = c[col].astype(float).values
    f["primary_align"] = c["primary_trend"].values * side.values
    f["risk_atr"] = ((close - s["sl"]) * side / atr_).values
    f["reward_risk"] = ((s["tp"] - close) / (close - s["sl"])).values
    f["hour_sin"], f["hour_cos"] = np.sin(2 * np.pi * hours / 24), np.cos(2 * np.pi * hours / 24)
    f["dow"] = s.index.dayofweek.astype(float)
    f["atr_pct"] = (atr_ / close).values
    return f[FEATURES].replace([np.inf, -np.inf], np.nan).fillna(0.0)


def purged_walk_forward_splits(entry: np.ndarray, exit_: np.ndarray, n_splits: int = 5,
                               embargo: pd.Timedelta = pd.Timedelta(hours=24), min_train: int = 100):
    """Yield (train_idx, test_idx). Samples must be sorted by entry time.

    The test blocks are consecutive chunks of the later part of the sample.
    A training sample is kept only if its label was fully known (trade
    closed) at least ``embargo`` before the first test entry."""
    n = len(entry)
    first = max(min_train, n // (n_splits + 1))
    edges = np.linspace(first, n, n_splits + 1).astype(int)
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        test = np.arange(a, b)
        cutoff = entry[a] - embargo.to_timedelta64()
        train = np.flatnonzero((np.arange(n) < a) & (exit_ <= cutoff))
        if len(train) >= min_train:
            yield train, test


def _auc(y: np.ndarray, p: np.ndarray) -> float:
    if len(np.unique(y)) < 2:
        return float("nan")
    from sklearn.metrics import roc_auc_score

    return float(roc_auc_score(y, p))


def _new_estimator(seed: int):
    from sklearn.ensemble import HistGradientBoostingClassifier

    # Small, heavily regularised model: the sample of trades is tiny by ML standards.
    return HistGradientBoostingClassifier(max_depth=3, learning_rate=0.05, max_iter=120, min_samples_leaf=25,
                                          l2_regularization=1.0, random_state=seed)


@dataclass
class MetaReport:
    strategy: str
    samples: int
    validated: bool
    reasons: list[str] = field(default_factory=list)
    oos_auc: float = float("nan")
    baseline_expectancy_r: float = float("nan")
    filtered_expectancy_r: float = float("nan")
    kept_fraction: float = float("nan")
    folds: list[dict] = field(default_factory=list)
    trained_until: str = ""
    features: list[str] = field(default_factory=lambda: list(FEATURES))
    sha256: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class MetaModel:
    def __init__(self, strategy: str, seed: int = 42) -> None:
        self.strategy = strategy
        self.seed = seed
        self.model = None
        self.report: Optional[MetaReport] = None

    # ------------------------------------------------------------------ train
    def fit(self, features: pd.DataFrame, r_multiple: np.ndarray, entry: np.ndarray, exit_: np.ndarray,
            n_splits: int = 5) -> MetaReport:
        """Validate with purged walk-forward CV, then fit on everything."""
        order = np.argsort(entry, kind="stable")
        X = features[FEATURES].values[order]
        r = np.asarray(r_multiple, float)[order]
        y = (r > 0).astype(int)
        entry, exit_ = np.asarray(entry)[order], np.asarray(exit_)[order]
        rep = MetaReport(strategy=self.strategy, samples=len(y), validated=False,
                         baseline_expectancy_r=float(r.mean()) if len(r) else float("nan"))
        self.report = rep
        if len(y) < MIN_SAMPLES:
            rep.reasons.append(f"only {len(y)} labelled trades (< {MIN_SAMPLES})")
            return rep
        if len(np.unique(y)) < 2:
            rep.reasons.append("only one outcome class in the sample")
            return rep

        oos_p = np.full(len(y), np.nan)
        for k, (tr, te) in enumerate(purged_walk_forward_splits(entry, exit_, n_splits)):
            if len(np.unique(y[tr])) < 2:
                continue
            est = _new_estimator(self.seed + k).fit(X[tr], y[tr])
            p = est.predict_proba(X[te])[:, 1]
            oos_p[te] = p
            keep = p >= KEEP_THRESHOLD
            rep.folds.append({
                "fold": k, "train": int(len(tr)), "test": int(len(te)), "auc": _auc(y[te], p),
                "baseline_r": float(r[te].mean()), "kept": int(keep.sum()),
                "filtered_r": float(r[te][keep].mean()) if keep.sum() >= 5 else float("nan")})
        have = np.isfinite(oos_p)
        if have.sum() < MIN_SAMPLES // 2 or not rep.folds:
            rep.reasons.append("not enough out-of-sample predictions")
            return rep
        rep.oos_auc = _auc(y[have], oos_p[have])
        keep = have & (oos_p >= KEEP_THRESHOLD)
        rep.kept_fraction = float(keep.sum() / have.sum())
        rep.baseline_expectancy_r = float(r[have].mean())
        rep.filtered_expectancy_r = float(r[keep].mean()) if keep.sum() >= 20 else float("nan")
        wins = [f["filtered_r"] > f["baseline_r"] for f in rep.folds if f["filtered_r"] == f["filtered_r"]]
        win_frac = float(np.mean(wins)) if wins else 0.0

        if not rep.oos_auc >= MIN_AUC:
            rep.reasons.append(f"out-of-sample AUC {rep.oos_auc:.3f} < {MIN_AUC}")
        if not rep.filtered_expectancy_r > rep.baseline_expectancy_r:
            rep.reasons.append("filtered trades do not beat the unfiltered baseline out of sample")
        if win_frac < MIN_FOLD_WIN_FRACTION:
            rep.reasons.append(f"improves expectancy in only {win_frac:.0%} of folds")
        if rep.kept_fraction < 0.2:
            rep.reasons.append("model would discard more than 80% of trades")
        rep.validated = not rep.reasons
        self.model = _new_estimator(self.seed).fit(X, y)
        rep.trained_until = str(pd.Timestamp(exit_.max()))
        return rep

    # ---------------------------------------------------------------- predict
    @property
    def usable(self) -> bool:
        return self.model is not None and self.report is not None and self.report.validated

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("model is not trained")
        return self.model.predict_proba(features[FEATURES].values)[:, 1]

    # ------------------------------------------------------------ persistence
    def save(self, directory: str | Path) -> Path:
        import joblib

        if self.model is None or self.report is None:
            raise RuntimeError("nothing to save")
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"meta_{self.strategy}.joblib"
        joblib.dump(self.model, path)
        self.report.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        (d / f"meta_{self.strategy}.json").write_text(json.dumps(self.report.to_dict(), indent=2, default=str))
        return path

    @classmethod
    def load(cls, directory: str | Path, strategy: str) -> Optional["MetaModel"]:
        """Load a model if present AND its file hash matches its report."""
        import joblib

        d = Path(directory)
        path, meta = d / f"meta_{strategy}.joblib", d / f"meta_{strategy}.json"
        if not path.exists() or not meta.exists():
            return None
        info = json.loads(meta.read_text())
        if hashlib.sha256(path.read_bytes()).hexdigest() != info.get("sha256"):
            raise RuntimeError(f"model file {path.name} does not match its recorded hash - refusing to load")
        m = cls(strategy)
        m.report = MetaReport(**{k: v for k, v in info.items() if k in MetaReport.__dataclass_fields__})
        m.model = joblib.load(path)
        return m
