from __future__ import annotations

import math
from typing import Any

from wallex_trader.config import Settings
from wallex_trader.models import Candidate, Side
from wallex_trader.strategy.regime import RegimeState


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class SignalEngine:
    """Deterministic candidate generation.

    The deterministic layer only nominates trades. Every entry must still be
    approved by the DeepSeek agent and the risk kernel.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        symbol: str,
        features: dict[str, Any],
        regime: RegimeState,
        *,
        fee_taker: float | None = None,
        fee_maker: float | None = None,
    ) -> Candidate | None:
        if regime.label in ("illiquid", "chaos", "trend_down"):
            return None
        price = _num(features.get("price"))
        spread = _num(features.get("spread_bps"), 10_000)
        top5_depth = _num(features.get("top5_depth_tmn"), 0)
        if price <= 0 or spread > self.settings.max_spread_to_trade_bps:
            return None
        if top5_depth < self.settings.max_order_notional_tmn * 5:
            return None

        z = _num(features.get("bb_z_20"))
        rsi = _num(features.get("rsi_14"), 50)
        ret_1m = _num(features.get("ret_1m"))
        ret_5m = _num(features.get("ret_5m"))
        ret_15m = _num(features.get("ret_15m"))
        ret_1h = _num(features.get("ret_1h"))
        volume_z = _num(features.get("volume_z_60"))
        imbalance = _num(features.get("book_imbalance_5"))
        buy_ratio = _num(features.get("aggressive_buy_ratio"), 0.5)
        btc_15m = _num(features.get("btc_ret_15m"))
        atr_bps = _num(features.get("atr_bps"), 10)
        rv_1m_bps = _num(features.get("rv_1m_bps"), atr_bps / 1.5)
        ema_dist = _num(features.get("ema21_dist_bps"))
        ema50_dist = _num(features.get("ema50_dist_bps"))
        ret_4h_bps = _num(features.get("ret_4h_bps"))
        btc_ret_1h = _num(features.get("btc_ret_1h"))
        symbol_vs_btc = _num(features.get("symbol_vs_btc_15m_bps"))
        vwap_dist = _num(features.get("price_vs_vwap_bps"))

        mr_raw = 0.0
        mr_reason = ""
        mr_regime_ok = (
            ema50_dist > -250
            and ret_4h_bps > -400
            and btc_15m > -60
            and symbol_vs_btc > -250
        )
        if (
            z <= -self.settings.z_entry
            and rsi <= 38
            and vwap_dist <= 0
            and mr_regime_ok
        ):
            stretch = _clamp((abs(z) - self.settings.z_entry) / self.settings.z_entry, 0, 1.5)
            mr_raw = 0.38 + 0.32 * min(1.0, stretch) + 0.12 * (rsi < 30)
            mr_raw += 0.10 * max(0.0, imbalance) + 0.08 * max(0.0, buy_ratio - 0.45) * 2
            mr_raw = _clamp(mr_raw, 0, 1)
            mr_reason = (
                f"mean-reversion: z={z:.2f}, RSI={rsi:.0f}, "
                f"VWAP={vwap_dist:+.0f}bps, book imbalance={imbalance:+.2f}"
            )

        mom_raw = 0.0
        mom_reason = ""
        alignment = sum(1 for value in (ret_5m, ret_15m, ret_1h) if value > 0)
        mom_regime_ok = (
            rsi <= 75
            and z <= 2.0
            and ret_4h_bps > -150
            and btc_15m > -30
            and btc_ret_1h > -80
            and ret_1m <= 0.003
        )
        if (
            ret_5m > 0
            and ret_15m > 0
            and alignment >= 2
            and ema_dist > 0
            and volume_z > 0.5
            and mom_regime_ok
        ):
            mom_raw = 0.30 * (alignment / 3)
            mom_raw += 0.20 * _clamp(volume_z / 3.0, 0, 1)
            mom_raw += 0.20 * _clamp(imbalance + 0.5, 0, 1)
            mom_raw += 0.15 * _clamp(btc_15m / 100.0 + 0.5, 0, 1)
            mom_raw += 0.15 * _clamp(ema_dist / 100.0, 0, 1)
            mom_raw = _clamp(mom_raw, 0, 1)
            mom_reason = (
                f"momentum: 5m={ret_5m * 10_000:+.0f}bps, 15m={ret_15m * 10_000:+.0f}bps, "
                f"volume_z={volume_z:.1f}, imbalance={imbalance:+.2f}, BTC15m={btc_15m:+.0f}bps"
            )

        if mr_raw <= 0 and mom_raw <= 0:
            return None
        signal = "MR" if mr_raw >= mom_raw else "MOM"
        raw_score = max(mr_raw, mom_raw)
        fit = regime.fit(signal)
        if fit <= 0:
            return None
        score = raw_score * (0.55 + 0.45 * fit)
        if score < self.settings.candidate_threshold:
            return None

        # Volatility-scaled expected move over a 15-60 minute horizon.
        sigma_15 = max(1.0, rv_1m_bps * math.sqrt(15))
        sigma_60 = max(1.0, rv_1m_bps * math.sqrt(60))
        if signal == "MR":
            gross_move = max(0.65 * abs(z) * sigma_15, 0.45 * sigma_60, 0.9 * atr_bps)
            gross_move *= 0.9 + 0.1 * fit
        else:
            gross_move = max(
                abs(ret_15m) * 10_000 * 0.55 + 0.25 * sigma_60,
                0.7 * atr_bps,
                0.35 * sigma_60,
            )
            gross_move *= 0.85 + 0.15 * fit
        gross_move = _clamp(gross_move, 1.0, 500.0)

        maker = fee_maker if fee_maker is not None else self.settings.maker_fee
        taker = fee_taker if fee_taker is not None else self.settings.taker_fee
        taker_bps = 2 * (taker if taker > 0 else self.settings.taker_fee) * 10_000
        maker_bps = 2 * (maker if maker > 0 else self.settings.maker_fee) * 10_000
        # Conservative hurdle assumes taker entry + taker exit. The agent can
        # choose passive entry and improve economics, never worsen the hurdle.
        cost_bps = spread + taker_bps + self.settings.max_slippage_bps * 0.4 + self.settings.cost_buffer_bps
        net_edge = gross_move - cost_bps
        if net_edge < self.settings.alpha_threshold_bps:
            return None
        if gross_move < self.settings.min_expected_move_bps:
            return None

        stop_bps = _clamp(
            max(self.settings.stop_loss_bps, 1.5 * atr_bps),
            self.settings.min_stop_bps,
            self.settings.max_stop_bps,
        )
        take_bps = _clamp(
            max(self.settings.take_profit_bps, 1.6 * stop_bps, 1.2 * sigma_60),
            self.settings.min_take_profit_bps,
            self.settings.max_take_profit_bps,
        )
        horizon = int(_clamp(30 * 60 * (sigma_60 / max(1.0, sigma_15)), 900, 7200))

        features_out = {
            key: features.get(key)
            for key in (
                "price",
                "ret_1m",
                "ret_5m",
                "ret_15m",
                "ret_1h",
                "rsi_14",
                "bb_z_20",
                "atr_bps",
                "rv_1m_bps",
                "ret_4h_bps",
                "rv_percentile",
                "volume_z_60",
                "price_vs_vwap_bps",
                "ema21_dist_bps",
                "ema50_dist_bps",
                "ema50_slope_bps",
                "spread_bps",
                "book_imbalance_5",
                "aggressive_buy_ratio",
                "top5_depth_tmn",
                "btc_ret_5m",
                "btc_ret_15m",
                "btc_ret_1h",
                "symbol_vs_btc_15m_bps",
                "usdtmn_ret_5m",
                "beta_btc",
                "corr_btc",
                "book_age_ms",
            )
        }
        features_out["hurdle_cost_bps"] = round(cost_bps, 2)
        features_out["maker_roundtrip_bps"] = round(maker_bps, 2)
        return Candidate(
            symbol=symbol,
            side=Side.BUY,
            signal=signal,
            score=round(score, 4),
            expected_edge_bps=round(net_edge, 2),
            spread_bps=round(spread, 2),
            top5_depth_tmn=top5_depth,
            features=features_out,
            regime=regime.label,
            rationale=f"{mr_reason if signal == 'MR' else mom_reason}; net edge {net_edge:.0f} bps after costs",
            expected_move_bps=round(gross_move, 2),
            recommended_stop_bps=round(stop_bps, 1),
            recommended_take_profit_bps=round(take_bps, 1),
            recommended_horizon_sec=horizon,
        )


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
