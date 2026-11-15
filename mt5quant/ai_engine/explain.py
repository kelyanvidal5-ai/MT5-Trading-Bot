"""Human-readable explanations of trading decisions.

Explanations are assembled only from values the engine actually computed
(regime metrics, confidence components, risk numbers, calendar entries). The
explainer never generates prices, forecasts or news of its own.
"""
from __future__ import annotations

from typing import Optional

from ..core.models import Signal

_REGIME_TEXT = {
    "trend_up": "orderly up-trend", "trend_down": "orderly down-trend", "range": "range-bound",
    "quiet": "compressed volatility", "volatile": "disorderly high volatility", "unknown": "unknown regime",
}


def explain(signal: Signal, *, action: str, regime: str, components: Optional[dict] = None,
            confidence: float = 0.0, min_confidence: float = 0.0, risk: Optional[dict] = None,
            rejections: Optional[list[str]] = None, context: Optional[dict] = None,
            news: Optional[str] = None, digits: int = 5) -> str:
    c = components or {}
    ctx = context or {}
    lines = [f"{action.upper()} {signal.side.value.upper()} {signal.symbol} via {signal.strategy} "
             f"({signal.meta.get('style', '')}, {signal.timeframe})"]
    lines.append(f"Hypothesis: {signal.reasons[0] if signal.reasons else 'n/a'}")
    metrics = []
    if ctx.get("adx_h1") is not None:
        metrics.append(f"ADX {ctx['adx_h1']:.0f}")
    if ctx.get("er_h1") is not None:
        metrics.append(f"efficiency {ctx['er_h1']:.2f}")
    if ctx.get("vol_rank") is not None:
        metrics.append(f"volatility rank {ctx['vol_rank']:.0%}")
    lines.append(f"Regime (H1): {_REGIME_TEXT.get(regime, regime)}" + (f" [{', '.join(metrics)}]" if metrics else ""))
    trends = [f"{tf} {'up' if v > 0 else 'down' if v < 0 else 'flat'}"
              for tf, v in (ctx.get("trends") or {}).items()]
    if trends:
        lines.append("Timeframes: " + ", ".join(trends))
    if c:
        parts = [f"strategy strength {c.get('c_strength', 0):.2f}", f"regime fit {c.get('c_regime', 0):.2f}",
                 f"timeframe agreement {c.get('c_timeframes', 0):.2f}", f"cost {c.get('c_cost', 0):.2f}",
                 f"news {c.get('c_news', 0):.2f}"]
        if c.get("meta_prob") is not None:
            parts.append(f"ML probability {c['meta_prob']:.2f}")
        lines.append(f"Confidence {confidence:.2f} (minimum {min_confidence:.2f}): " + ", ".join(parts))
    lines.append(f"Levels: reference {signal.ref_price:.{digits}f}, stop {signal.sl:.{digits}f}, "
                 f"target {signal.tp:.{digits}f}, reward/risk {signal.reward_risk:.2f}")
    if risk:
        lines.append(f"Risk: {risk.get('volume', 0):.2f} lots, {risk.get('risk_amount', 0):.2f} "
                     f"({risk.get('risk_pct', 0):.2f}% of equity)"
                     + (f", scaled x{risk['scale']:.2f}: {risk['scale_reason']}" if risk.get("scale_reason") else ""))
    if news:
        lines.append(f"Calendar: {news}")
    if rejections:
        lines.append("Not traded because: " + "; ".join(rejections))
    return "\n".join(lines)
