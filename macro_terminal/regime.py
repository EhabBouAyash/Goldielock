"""Macro regime interpretation from market data and intraday confluence."""

from __future__ import annotations

from typing import Any

from macro_terminal.models import GoldBias, MacroRegimeState


def _to_float(value: Any) -> float | None:
    try:
        if value in (None, "", "n/a", "N/A"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def interpret_regime(market: dict[str, Any], intraday: dict[str, Any] | None = None) -> MacroRegimeState:
    gold = market.get("gold_spot") or {}
    dxy = market.get("dxy") or {}
    real_yield = _to_float(market.get("real_yield"))
    breakeven = _to_float(market.get("breakeven"))
    treasury = market.get("treasury") or {}

    dxy_change = _to_float(dxy.get("change_pct"))
    treasury_bps = _to_float(treasury.get("y10_daily_change_bps") or treasury.get("change_bps_1d"))

    tensor = (intraday or {}).get("confluence_tensor") or {}
    confluence_score = _to_float(tensor.get("aggregate_score"))
    confluence_label = str(tensor.get("alignment_label") or "")

    drivers: list[str] = []
    regime_scores: dict[str, float] = {
        "USD_STRONG": 0.0,
        "USD_WEAK": 0.0,
        "RISK_OFF": 0.0,
        "RISK_ON": 0.0,
        "INFLATION_UP": 0.0,
        "INFLATION_DOWN": 0.0,
        "NEUTRAL": 0.0,
    }

    if dxy_change is not None:
        if dxy_change > 0.15:
            regime_scores["USD_STRONG"] += 2.0
            drivers.append(f"DXY +{dxy_change:.2f}%")
        elif dxy_change < -0.15:
            regime_scores["USD_WEAK"] += 2.0
            drivers.append(f"DXY {dxy_change:.2f}%")

    if treasury_bps is not None:
        if treasury_bps > 3:
            regime_scores["USD_STRONG"] += 1.0
            regime_scores["INFLATION_UP"] += 0.5
            drivers.append(f"10Y +{treasury_bps:.1f}bps")
        elif treasury_bps < -3:
            regime_scores["USD_WEAK"] += 1.0
            regime_scores["INFLATION_DOWN"] += 0.5
            drivers.append(f"10Y {treasury_bps:.1f}bps")

    if real_yield is not None:
        if real_yield >= 2.0:
            regime_scores["USD_STRONG"] += 1.0
            drivers.append(f"real yield {real_yield:.2f}%")
        elif real_yield <= 0.5:
            regime_scores["USD_WEAK"] += 1.0
            regime_scores["RISK_OFF"] += 0.5
            drivers.append(f"real yield {real_yield:.2f}%")

    if breakeven is not None:
        if breakeven >= 2.4:
            regime_scores["INFLATION_UP"] += 1.0
            drivers.append(f"breakeven {breakeven:.2f}%")
        elif breakeven <= 2.0:
            regime_scores["INFLATION_DOWN"] += 1.0

    if confluence_score is not None:
        if confluence_score >= 3:
            regime_scores["RISK_OFF"] += 1.0
            drivers.append(f"confluence {confluence_score:+.1f}")
        elif confluence_score <= -3:
            regime_scores["RISK_ON"] += 1.0
            drivers.append(f"confluence {confluence_score:+.1f}")

    primary = max(regime_scores, key=regime_scores.get)
    top_score = regime_scores[primary]
    if top_score < 1.0:
        primary = "NEUTRAL"

    gold_bias = _gold_bias_from_regime(primary, confluence_label, dxy_change, treasury_bps)
    clarity = min(10, int(top_score * 2) + (2 if confluence_label else 0))
    narrative = _build_narrative(primary, gold_bias, drivers, confluence_label)

    return MacroRegimeState(
        primary_regime=primary,
        gold_bias=gold_bias,
        clarity=clarity,
        narrative=narrative,
        drivers=drivers[:6],
        confluence_label=confluence_label,
        confluence_score=confluence_score,
    )


def _gold_bias_from_regime(
    primary: str,
    confluence_label: str,
    dxy_change: float | None,
    treasury_bps: float | None,
) -> str:
    bullish = {"USD_WEAK", "RISK_OFF", "INFLATION_UP"}
    bearish = {"USD_STRONG", "RISK_ON", "INFLATION_DOWN"}

    if primary in bullish:
        bias = GoldBias.BULLISH.value
    elif primary in bearish:
        bias = GoldBias.BEARISH.value
    else:
        bias = GoldBias.NEUTRAL.value

    if "BULLISH" in confluence_label and bias == GoldBias.BEARISH.value:
        return GoldBias.TWO_WAY.value
    if "BEARISH" in confluence_label and bias == GoldBias.BULLISH.value:
        return GoldBias.TWO_WAY.value

    if dxy_change is not None and treasury_bps is not None:
        if dxy_change > 0.2 and treasury_bps > 4 and bias == GoldBias.BULLISH.value:
            return GoldBias.TWO_WAY.value
        if dxy_change < -0.2 and treasury_bps < -4 and bias == GoldBias.BEARISH.value:
            return GoldBias.TWO_WAY.value

    return bias


def _build_narrative(
    primary: str,
    gold_bias: str,
    drivers: list[str],
    confluence_label: str,
) -> str:
    driver_text = ", ".join(drivers[:3]) if drivers else "mixed macro inputs"
    conf = f" Intraday tape: {confluence_label.replace('_', ' ').lower()}." if confluence_label else ""
    return (
        f"Primary regime {primary.replace('_', ' ').lower()} "
        f"({gold_bias.lower()} gold bias) on {driver_text}.{conf}"
    )
