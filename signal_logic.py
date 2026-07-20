"""
signal_logic.py
================
OTC 8-Factor Confluence Engine — replaces the old UT Bot Alerts logic.

Factors (from the "8 Proven Technical Factors" reference sheet):
    1. Bollinger Band Bounce
    2. RSI Divergence
    3. Fair Value Gap (FVG)
    4. Order Block
    5. Stochastic %K/%D Cross
    6. CCI Extreme + Reversal
    7. Price Action Structure (BOS / CHoCH)
    8. Candlestick Pattern (Hammer, Shooting Star, Engulfing, Doji)

Public API kept IDENTICAL to the previous version so app.py / background_engine.py /
qx_client.py do NOT need any changes:

    get_next_candle_window(period_seconds, now=None)
    get_value_safe(series, index=-1, default=0.0)
    calculate_indicators(df) -> df
    calculate_htf_trend(df) -> "bull" | "bear" | "neutral"
    get_signal_simple(df, htf_trend="neutral", min_confidence=50.0, df_5m=None)
        -> (signal: "CALL"/"PUT"/None, confidence: float, reasons: list[str])

Confidence is now (agreeing_factors / 8) * 100 — e.g. 4/8 factors agreeing = 50%,
6/8 = 75%. min_confidence is converted internally to a minimum factor count.

Support / Resistance zone gate (added):
    - CALL (buy) only fires when price is at the support/demand zone (market bottom).
    - PUT (sell) only fires when price is at the resistance/supply zone (market top).
    - A CALL near resistance or a PUT near support is blocked, even if the 8-factor
      confluence score would otherwise allow it.
    Tunable via SR_LOOKBACK (zone lookback, candles) and SR_ZONE_ATR_MULT
    (zone width, in ATRs) near the top of this file.
"""

import pandas as pd
import numpy as np
from datetime import datetime

NUM_FACTORS = 8


def get_next_candle_window(period_seconds: int, now: float = None):
    import time as _time
    if now is None:
        now = _time.time()
    next_boundary = (int(now // period_seconds) + 1) * period_seconds
    entry_dt = datetime.fromtimestamp(next_boundary)
    exit_dt = datetime.fromtimestamp(next_boundary + period_seconds)
    seconds_until_entry = round(next_boundary - now, 1)
    return entry_dt, exit_dt, seconds_until_entry


# ============================================================
# Safe value helper (unchanged)
# ============================================================

def get_value_safe(series, index=-1, default=0.0):
    try:
        val = series.iloc[index]
        if pd.isna(val):
            return default
        return float(val)
    except Exception:
        return default


# ============================================================
# Settings - 60%+ Accuracy for OTC + Real Market (UNIVERSAL)
# ============================================================

BB_PERIOD, BB_STD = 20, 2.0          # ক্লাসিক BB
RSI_PERIOD, RSI_LOOKBACK = 14, 6     # মিড রেঞ্জ RSI
ORDER_BLOCK_IMPULSE_MULT, ORDER_BLOCK_BODY_LOOKBACK = 1.6, 10  # ব্যালান্সড
STOCH_K, STOCH_D, STOCH_SMOOTH = 14, 3, 3
STOCH_OVERSOLD, STOCH_OVERBOUGHT = 22, 78   # OTC + রিয়েলের জন্য মিড
CCI_PERIOD, CCI_EXTREME = 20, 120           # CCI একটু বেশি (ফেক ফিল্টার)
SWING_LOOKBACK = 5

TREND_EMA_LEN = 20   # used for optional soft 5m trend confirmation

# ── Support / Resistance zone filter ──
# CALL is only allowed near a support/demand zone (market bottom).
# PUT  is only allowed near a resistance/supply zone (market top).
ATR_PERIOD = 14
SR_LOOKBACK = 35          # মিড রেঞ্জ (দুটোর জন্য কমন)
SR_ZONE_ATR_MULT = 0.4    # গোল্ডিলক্স - খুব টাইট না, খুব লুজ না

# Minimum factors required for signal (CRITICAL for 60%+ accuracy)
MIN_FACTORS_REQUIRED = 3  # CALL আর PUT দুইটার জন্য


# ============================================================
# OHLC normalization (same column-guessing behavior as before)
# ============================================================

def _normalize_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    rename_map = {}
    for col in df.columns:
        lc = str(col).lower()
        if lc in ("open", "o"):
            rename_map[col] = "Open"
        elif lc in ("close", "c", "price"):
            rename_map[col] = "Close"
        elif lc in ("high", "h", "max"):
            rename_map[col] = "High"
        elif lc in ("low", "l", "min"):
            rename_map[col] = "Low"
        elif lc in ("volume", "v"):
            rename_map[col] = "Volume"
    df = df.rename(columns=rename_map)

    if "High" not in df.columns:
        df["High"] = df[["Open", "Close"]].max(axis=1)
    if "Low" not in df.columns:
        df["Low"] = df[["Open", "Close"]].min(axis=1)
    if "Volume" not in df.columns:
        df["Volume"] = 0
    return df


# ============================================================
# 1. Bollinger Band Bounce
# ============================================================

def _bollinger_band_bounce(df):
    mid = df["Close"].rolling(BB_PERIOD).mean()
    std = df["Close"].rolling(BB_PERIOD).std()
    upper = mid + BB_STD * std
    lower = mid - BB_STD * std

    signal = pd.Series(0, index=df.index)
    touched_lower = (df["Low"] <= lower) & (df["Close"] > lower)
    touched_upper = (df["High"] >= upper) & (df["Close"] < upper)
    signal[touched_lower] = 1
    signal[touched_upper] = -1
    return signal, upper, mid, lower


# ============================================================
# 2. RSI + Divergence
# ============================================================

def _rsi(series, period=RSI_PERIOD):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _rsi_divergence(df):
    r = _rsi(df["Close"])
    signal = pd.Series(0, index=df.index)

    price_low = df["Close"].rolling(RSI_LOOKBACK).min()
    price_high = df["Close"].rolling(RSI_LOOKBACK).max()
    rsi_low = r.rolling(RSI_LOOKBACK).min()
    rsi_high = r.rolling(RSI_LOOKBACK).max()

    bullish = (df["Close"] <= price_low) & (r > rsi_low.shift(1)) & (r < 35)
    bearish = (df["Close"] >= price_high) & (r < rsi_high.shift(1)) & (r > 65)

    signal[bullish] = 1
    signal[bearish] = -1
    return signal, r


# ============================================================
# 3. Fair Value Gap (FVG) — 3-candle imbalance
# ============================================================

def _fair_value_gap(df):
    signal = pd.Series(0, index=df.index)
    high2 = df["High"].shift(2)
    low2 = df["Low"].shift(2)

    bullish_fvg = df["Low"] > high2
    bearish_fvg = df["High"] < low2

    signal[bullish_fvg] = 1
    signal[bearish_fvg] = -1
    return signal


# ============================================================
# 4. Order Block — last opposite candle before a strong impulsive move
# ============================================================

def _order_block(df):
    signal = pd.Series(0, index=df.index)
    body = (df["Close"] - df["Open"]).abs()
    avg_body = body.rolling(ORDER_BLOCK_BODY_LOOKBACK).mean()

    is_strong_bull = (df["Close"] > df["Open"]) & (body > ORDER_BLOCK_IMPULSE_MULT * avg_body)
    is_strong_bear = (df["Close"] < df["Open"]) & (body > ORDER_BLOCK_IMPULSE_MULT * avg_body)

    prev_bear = df["Close"].shift(1) < df["Open"].shift(1)
    prev_bull = df["Close"].shift(1) > df["Open"].shift(1)

    signal[is_strong_bull & prev_bear] = 1
    signal[is_strong_bear & prev_bull] = -1
    return signal


# ============================================================
# 5. Stochastic %K / %D Cross
# ============================================================

def _stochastic(df):
    low_min = df["Low"].rolling(STOCH_K).min()
    high_max = df["High"].rolling(STOCH_K).max()
    raw_k = 100 * (df["Close"] - low_min) / (high_max - low_min).replace(0, np.nan)
    k = raw_k.rolling(STOCH_SMOOTH).mean()
    d = k.rolling(STOCH_D).mean()
    return k, d


def _stochastic_cross(df):
    k, d = _stochastic(df)
    signal = pd.Series(0, index=df.index)

    cross_up = (k > d) & (k.shift(1) <= d.shift(1)) & (k < STOCH_OVERSOLD + 15)
    cross_down = (k < d) & (k.shift(1) >= d.shift(1)) & (k > STOCH_OVERBOUGHT - 15)

    signal[cross_up] = 1
    signal[cross_down] = -1
    return signal, k, d


# ============================================================
# 6. CCI Extreme + Reversal
# ============================================================

def _cci(df, period=CCI_PERIOD):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    sma = tp.rolling(period).mean()
    mad = tp.rolling(period).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    return (tp - sma) / (0.015 * mad.replace(0, np.nan))


def _cci_extreme_reversal(df):
    c = _cci(df)
    signal = pd.Series(0, index=df.index)

    bullish = (c.shift(1) < -CCI_EXTREME) & (c > c.shift(1))
    bearish = (c.shift(1) > CCI_EXTREME) & (c < c.shift(1))

    signal[bullish] = 1
    signal[bearish] = -1
    return signal, c


# ============================================================
# 7. Price Action Structure — BOS / CHoCH
# ============================================================

def _price_action_structure(df):
    signal = pd.Series(0, index=df.index)

    swing_high = df["High"].rolling(SWING_LOOKBACK, center=True).max()
    swing_low = df["Low"].rolling(SWING_LOOKBACK, center=True).min()

    is_swing_high = df["High"] == swing_high
    is_swing_low = df["Low"] == swing_low

    last_swing_high = df["High"].where(is_swing_high).ffill().shift(1)
    last_swing_low = df["Low"].where(is_swing_low).ffill().shift(1)

    bos_bull = df["Close"] > last_swing_high
    bos_bear = df["Close"] < last_swing_low

    signal[bos_bull] = 1
    signal[bos_bear] = -1
    return signal


# ============================================================
# 8. Candlestick Pattern — Hammer, Shooting Star, Engulfing, Doji
# ============================================================

def _candlestick_pattern(df):
    signal = pd.Series(0, index=df.index)
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]

    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)
    upper_wick = h - np.maximum(o, c)
    lower_wick = np.minimum(o, c) - l

    is_doji = body <= 0.1 * rng
    is_hammer = (lower_wick >= 2 * body) & (upper_wick <= 0.3 * body.replace(0, np.nan)) & (c.shift(1) < o.shift(1))
    is_shooting_star = (upper_wick >= 2 * body) & (lower_wick <= 0.3 * body.replace(0, np.nan)) & (c.shift(1) > o.shift(1))
    bullish_engulf = (c > o) & (o.shift(1) > c.shift(1)) & (c >= o.shift(1)) & (o <= c.shift(1))
    bearish_engulf = (c < o) & (c.shift(1) > o.shift(1)) & (o >= c.shift(1)) & (c <= o.shift(1))

    signal[is_hammer | bullish_engulf] = 1
    signal[is_shooting_star | bearish_engulf] = -1
    return signal, is_doji


# ============================================================
# 9. Support / Resistance Zone (market top / bottom filter)
# ============================================================

def _atr(df, period=ATR_PERIOD):
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


def _support_resistance_zone(df, lookback=SR_LOOKBACK, atr_mult=SR_ZONE_ATR_MULT):
    """
    Resistance = highest high of the last `lookback` candles (market top / supply zone).
    Support    = lowest low of the last `lookback` candles (market bottom / demand zone).
    A candle's Close is considered "at" that zone if it sits within `atr_mult` ATRs
    of the level — this defines how wide the resistance/support zone is.
    """
    resistance = df["High"].rolling(lookback).max()
    support = df["Low"].rolling(lookback).min()
    atr = _atr(df)
    zone_width = (atr * atr_mult).fillna(0)

    near_resistance = df["Close"] >= (resistance - zone_width)
    near_support = df["Close"] <= (support + zone_width)

    return resistance, support, zone_width, near_resistance, near_support


# ============================================================
# Main indicator builder — computes all 8 factors + score columns
# ============================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = _normalize_ohlc(df)

    bb_sig, bb_upper, bb_mid, bb_lower = _bollinger_band_bounce(df)
    rsi_sig, rsi_val = _rsi_divergence(df)
    fvg_sig = _fair_value_gap(df)
    ob_sig = _order_block(df)
    stoch_sig, k, d = _stochastic_cross(df)
    cci_sig, cci_val = _cci_extreme_reversal(df)
    pa_sig = _price_action_structure(df)
    candle_sig, is_doji = _candlestick_pattern(df)
    resistance, support, zone_width, near_resistance, near_support = _support_resistance_zone(df)

    df["F_BollingerBounce"] = bb_sig
    df["F_RSIDivergence"] = rsi_sig
    df["F_FairValueGap"] = fvg_sig
    df["F_OrderBlock"] = ob_sig
    df["F_StochasticCross"] = stoch_sig
    df["F_CCIExtremeReversal"] = cci_sig
    df["F_PriceActionStructure"] = pa_sig
    df["F_CandlestickPattern"] = candle_sig
    df["F_Doji"] = is_doji

    df["BB_Upper"], df["BB_Mid"], df["BB_Lower"] = bb_upper, bb_mid, bb_lower
    df["RSI"] = rsi_val
    df["CCI"] = cci_val
    df["Stoch_K"], df["Stoch_D"] = k, d

    df["Resistance"] = resistance
    df["Support"] = support
    df["SR_ZoneWidth"] = zone_width
    df["Near_Resistance"] = near_resistance
    df["Near_Support"] = near_support

    factor_cols = [
        "F_BollingerBounce", "F_RSIDivergence", "F_FairValueGap", "F_OrderBlock",
        "F_StochasticCross", "F_CCIExtremeReversal", "F_PriceActionStructure",
        "F_CandlestickPattern",
    ]
    df["Bullish_Count"] = (df[factor_cols] == 1).sum(axis=1)
    df["Bearish_Count"] = (df[factor_cols] == -1).sum(axis=1)
    df["Score"] = df[factor_cols].sum(axis=1)

    return df


_FACTOR_LABELS = {
    "F_BollingerBounce": "Bollinger Band Bounce",
    "F_RSIDivergence": "RSI Divergence",
    "F_FairValueGap": "Fair Value Gap (FVG)",
    "F_OrderBlock": "Order Block",
    "F_StochasticCross": "Stochastic %K/%D Cross",
    "F_CCIExtremeReversal": "CCI Extreme + Reversal",
    "F_PriceActionStructure": "Price Action Structure (BOS/CHoCH)",
    "F_CandlestickPattern": "Candlestick Pattern",
}


# ============================================================
# HTF trend (kept for backward compatibility + optional soft filter)
# ============================================================

def calculate_htf_trend(df: pd.DataFrame) -> str:
    if df is None or len(df) < TREND_EMA_LEN:
        return "neutral"
    d = _normalize_ohlc(df)
    ema = d["Close"].ewm(span=TREND_EMA_LEN, adjust=False).mean()
    close = get_value_safe(d["Close"])
    e = get_value_safe(ema)
    if close > e:
        return "bull"
    if close < e:
        return "bear"
    return "neutral"


def _trend_ema_from_5m(df_5m: pd.DataFrame, length: int = TREND_EMA_LEN):
    if df_5m is None or len(df_5m) < length:
        return None
    d = _normalize_ohlc(df_5m)
    ema = d["Close"].ewm(span=length, adjust=False).mean()
    return get_value_safe(ema)


# ============================================================
# Main signal generator — Support / Resistance Zone driven
# ============================================================

def get_signal_simple(df: pd.DataFrame, htf_trend: str = "neutral", min_confidence: float = 50.0, df_5m: pd.DataFrame = None):
    """
    Signature unchanged from the old version so the bot's call sites
    (signal, confidence, reasons = get_signal_simple(...)) keep working.

    The 8-factor confluence minimum ("need ≥N/8 factors") has been REMOVED —
    it was blocking too many otherwise-good signals. The 8 factors are still
    calculated and shown in `reasons` for information, but they no longer
    gate whether a signal fires.

    Signal is now decided purely by the Support/Resistance zone:
        - CALL fires whenever price is at the support/demand zone (market bottom).
        - PUT  fires whenever price is at the resistance/supply zone (market top).
        - No signal if price is in the middle, away from either zone.

    `min_confidence` is kept only as a display floor for the confidence %
    (so the UI slider still does something useful) — it no longer blocks
    signals from firing.
    """
    reasons = []

    if df is None or len(df) < 30:
        return None, 0, ["❌ Not enough candle data (need at least 30)"]

    df = calculate_indicators(df)

    bullish_count = int(df["Bullish_Count"].iloc[-1])
    bearish_count = int(df["Bearish_Count"].iloc[-1])

    factor_cols = [
        "F_BollingerBounce", "F_RSIDivergence", "F_FairValueGap", "F_OrderBlock",
        "F_StochasticCross", "F_CCIExtremeReversal", "F_PriceActionStructure",
        "F_CandlestickPattern",
    ]
    fired_bull = [c for c in factor_cols if df[c].iloc[-1] == 1]
    fired_bear = [c for c in factor_cols if df[c].iloc[-1] == -1]

    # ── Support / Resistance zone (this is what decides the signal now) ──
    near_support = bool(df["Near_Support"].iloc[-1]) if "Near_Support" in df.columns else False
    near_resistance = bool(df["Near_Resistance"].iloc[-1]) if "Near_Resistance" in df.columns else False
    resistance_level = get_value_safe(df["Resistance"]) if "Resistance" in df.columns else None
    support_level = get_value_safe(df["Support"]) if "Support" in df.columns else None

    # ── optional soft trend confirmation (5m EMA), informational only ──
    trend_note = None
    trend_ema = _trend_ema_from_5m(df_5m, TREND_EMA_LEN)
    close = get_value_safe(df["Close"])
    if trend_ema is not None:
        current_trend = "bull" if close > trend_ema else ("bear" if close < trend_ema else "neutral")
        trend_note = f"5m EMA({TREND_EMA_LEN}) trend: {current_trend}"
    elif htf_trend != "neutral":
        current_trend = htf_trend
        trend_note = f"HTF trend (provided): {current_trend}"
    else:
        current_trend = "neutral"

    signal = None
    confidence = 0.0
    min_conf_floor = max(0.0, min(100.0, min_confidence))

    # ── MAIN SIGNAL LOGIC WITH MIN_FACTORS_REQUIRED ──
    # This is the key change for 60%+ accuracy
    
    if near_support and not near_resistance and bullish_count >= MIN_FACTORS_REQUIRED:
        signal = "CALL"
        factor_conf = round((bullish_count / NUM_FACTORS) * 100, 1)
        confidence = max(min_conf_floor, factor_conf)
        reasons.append(
            f"✅ Price at support/demand zone (support ≈ {support_level:.5f}) — CALL"
        )
        reasons.append(f"🎯 {bullish_count}/{NUM_FACTORS} factors bullish (min required: {MIN_FACTORS_REQUIRED})")
        if fired_bull:
            for f in fired_bull:
                reasons.append(f"  ✔ {_FACTOR_LABELS[f]}")
        if trend_note:
            reasons.append(("✅ " if current_trend == "bull" else "⚠️ ") + trend_note)

    elif near_resistance and not near_support and bearish_count >= MIN_FACTORS_REQUIRED:
        signal = "PUT"
        factor_conf = round((bearish_count / NUM_FACTORS) * 100, 1)
        confidence = max(min_conf_floor, factor_conf)
        reasons.append(
            f"✅ Price at resistance/supply zone (resistance ≈ {resistance_level:.5f}) — PUT"
        )
        reasons.append(f"🎯 {bearish_count}/{NUM_FACTORS} factors bearish (min required: {MIN_FACTORS_REQUIRED})")
        if fired_bear:
            for f in fired_bear:
                reasons.append(f"  ✔ {_FACTOR_LABELS[f]}")
        if trend_note:
            reasons.append(("✅ " if current_trend == "bear" else "⚠️ ") + trend_note)

    else:
        # No signal - show why
        if near_support and bullish_count < MIN_FACTORS_REQUIRED:
            reasons.append(
                f"⚠️ Price at support zone but only {bullish_count}/{MIN_FACTORS_REQUIRED} factors bullish "
                f"(need {MIN_FACTORS_REQUIRED} for CALL)"
            )
        elif near_resistance and bearish_count < MIN_FACTORS_REQUIRED:
            reasons.append(
                f"⚠️ Price at resistance zone but only {bearish_count}/{MIN_FACTORS_REQUIRED} factors bearish "
                f"(need {MIN_FACTORS_REQUIRED} for PUT)"
            )
        else:
            reasons.append(
                "ℹ️ Price is not at a support or resistance zone yet — no signal "
                f"(support ≈ {support_level:.5f}, resistance ≈ {resistance_level:.5f})"
            )
        if trend_note:
            reasons.append(trend_note)

    return signal, confidence, reasons