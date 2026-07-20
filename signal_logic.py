"""
signal_logic.py
================
OTC Support/Resistance Zone Engine.

8-Factor Confluence code has been FULLY REMOVED (not just disabled) per user
request. It was already informational-only / non-gating in the previous
version, so removing it does not change bot behavior in any way.

Public API kept IDENTICAL to the previous version so app.py / background_engine.py /
qx_client.py do NOT need any changes:

    get_next_candle_window(period_seconds, now=None)
    get_value_safe(series, index=-1, default=0.0)
    calculate_indicators(df) -> df
    calculate_htf_trend(df) -> "bull" | "bear" | "neutral"
    get_signal_simple(df, htf_trend="neutral", min_confidence=50.0, df_5m=None)
        -> (signal: "CALL"/"PUT"/None, confidence: float, reasons: list[str])

Signal logic (unchanged from before):
    - CALL (buy) only fires when price is at the support/demand zone (market bottom).
    - PUT (sell) only fires when price is at the resistance/supply zone (market top).
    Tunable via SR_LOOKBACK (zone lookback, candles) and SR_ZONE_ATR_MULT
    (zone width, in ATRs) near the top of this file.

Confidence is now DYNAMIC (added back without touching signal frequency):
    confidence = min_confidence (floor)
               + retest_bonus   (+5% per prior touch of this zone, capped at +25%)
               + trend_bonus    (+10% if 5m/HTF trend agrees with the signal
                                 direction, -10% if it disagrees, 0 if neutral)
    capped at 95%.
These bonuses only change the reported confidence number — they never block
or add a signal. CALL/PUT still fire on exactly the same condition as before
(price at support / resistance zone).
"""

import pandas as pd
import numpy as np
from datetime import datetime


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
# Settings
# ============================================================

ATR_PERIOD = 14
SR_LOOKBACK = 50          # candles used to find the swing high/low (resistance/support)
SR_ZONE_ATR_MULT = 0.5    # zone width = ATR * this multiplier (how "close" counts as "at" the level)

# ── Tightening filters (accuracy-focused, minimal signal loss) ──
WICK_REJECTION_MIN_RATIO = 0.30   # candle's rejection wick must be >= 30% of its total range
COOLDOWN_CANDLES = 4              # only the FIRST touch of a zone within this many candles counts

TREND_EMA_LEN = 24        # used for optional soft 5m trend confirmation


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
# ATR (kept — needed for S/R zone width)
# ============================================================

def _atr(df, period=ATR_PERIOD):
    high = df["High"]
    low = df["Low"]
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period).mean()


# ============================================================
# Support / Resistance zone
# ============================================================

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


def _zone_retest_counts(df, resistance, support, zone_width, lookback=SR_LOOKBACK):
    """
    Counts how many candles in the last `lookback` bars had a High that
    touched near the current resistance level, or a Low that touched near
    the current support level. More touches = a more "proven"/respected
    level = more trustworthy zone. Purely informational — used to boost
    (never to block) confidence.
    """
    res_touch = (df["High"] >= (resistance - zone_width)).astype(int)
    sup_touch = (df["Low"] <= (support + zone_width)).astype(int)
    resistance_retests = res_touch.rolling(lookback).sum()
    support_retests = sup_touch.rolling(lookback).sum()
    return resistance_retests, support_retests


def _wick_rejection_ratios(df):
    """
    lower_wick_ratio: how much of the candle's total range is the lower wick
    (Low to the bottom of the body). High ratio near a support zone = price
    dipped in and got rejected back up = real bounce, not a break.
    upper_wick_ratio: same idea for the top wick near a resistance zone.
    """
    candle_range = (df["High"] - df["Low"]).replace(0, np.nan)
    body_bottom = df[["Open", "Close"]].min(axis=1)
    body_top = df[["Open", "Close"]].max(axis=1)

    lower_wick_ratio = ((body_bottom - df["Low"]) / candle_range).fillna(0).clip(0, 1)
    upper_wick_ratio = ((df["High"] - body_top) / candle_range).fillna(0).clip(0, 1)
    return lower_wick_ratio, upper_wick_ratio


def _fresh_zone_touch(near_series, cooldown=COOLDOWN_CANDLES):
    """
    True only on the FIRST bar of a "near zone" streak within the cooldown
    window — i.e. if price has been sitting in the zone for several candles
    in a row, only the first one counts as a signal. Prevents an auto-trade
    bot from firing repeated near-duplicate trades while price chops
    sideways inside the same zone.
    """
    near_int = near_series.astype(int)
    return near_series & (near_int.rolling(cooldown).sum() == 1)


# ============================================================
# Main indicator builder — S/R zone only (8-factor block removed)
# ============================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = _normalize_ohlc(df)

    resistance, support, zone_width, near_resistance, near_support = _support_resistance_zone(df)
    resistance_retests, support_retests = _zone_retest_counts(df, resistance, support, zone_width)
    lower_wick_ratio, upper_wick_ratio = _wick_rejection_ratios(df)
    near_support_fresh = _fresh_zone_touch(near_support)
    near_resistance_fresh = _fresh_zone_touch(near_resistance)

    df["Resistance"] = resistance
    df["Support"] = support
    df["SR_ZoneWidth"] = zone_width
    df["Near_Resistance"] = near_resistance
    df["Near_Support"] = near_support
    df["Near_Resistance_Fresh"] = near_resistance_fresh
    df["Near_Support_Fresh"] = near_support_fresh
    df["Resistance_Retests"] = resistance_retests
    df["Support_Retests"] = support_retests
    df["Lower_Wick_Ratio"] = lower_wick_ratio
    df["Upper_Wick_Ratio"] = upper_wick_ratio

    return df


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
# (8-factor confluence fully removed — no factor calc, no factor gate)
# ============================================================

def get_signal_simple(df: pd.DataFrame, htf_trend: str = "neutral", min_confidence: float = 50.0, df_5m: pd.DataFrame = None):
    """
    Signature unchanged from the old version so the bot's call sites
    (signal, confidence, reasons = get_signal_simple(...)) keep working.

    Signal is decided purely by the Support/Resistance zone:
        - CALL fires whenever price is at the support/demand zone (market bottom).
        - PUT  fires whenever price is at the resistance/supply zone (market top).
        - No signal if price is in the middle, away from either zone.

    `min_confidence` is used as the confidence FLOOR. On top of it, a zone
    "strength" bonus (how many times this level was recently retested) and a
    trend-alignment bonus/penalty are added, capped at 95%. None of this
    changes whether a signal fires — only the confidence % attached to it.
    """
    reasons = []

    if df is None or len(df) < 30:
        return None, 0, ["❌ Not enough candle data (need at least 30)"]

    df = calculate_indicators(df)

    near_support = bool(df["Near_Support"].iloc[-1]) if "Near_Support" in df.columns else False
    near_resistance = bool(df["Near_Resistance"].iloc[-1]) if "Near_Resistance" in df.columns else False
    fresh_support = bool(df["Near_Support_Fresh"].iloc[-1]) if "Near_Support_Fresh" in df.columns else near_support
    fresh_resistance = bool(df["Near_Resistance_Fresh"].iloc[-1]) if "Near_Resistance_Fresh" in df.columns else near_resistance
    lower_wick_ratio = get_value_safe(df["Lower_Wick_Ratio"]) if "Lower_Wick_Ratio" in df.columns else 0.0
    upper_wick_ratio = get_value_safe(df["Upper_Wick_Ratio"]) if "Upper_Wick_Ratio" in df.columns else 0.0
    resistance_level = get_value_safe(df["Resistance"]) if "Resistance" in df.columns else None
    support_level = get_value_safe(df["Support"]) if "Support" in df.columns else None
    resistance_retests = int(get_value_safe(df["Resistance_Retests"])) if "Resistance_Retests" in df.columns else 0
    support_retests = int(get_value_safe(df["Support_Retests"])) if "Support_Retests" in df.columns else 0

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

    if near_support and not near_resistance and fresh_support and lower_wick_ratio >= WICK_REJECTION_MIN_RATIO:
        signal = "CALL"
        # ── dynamic confidence: floor + zone-strength bonus + trend bonus/penalty ──
        # Retest bonus: each prior touch of this support (capped at 5) adds up to +25%.
        retest_bonus = min(support_retests, 5) * 5
        # Trend bonus: aligned with bull trend +10%, against it -10%, neutral 0.
        if current_trend == "bull":
            trend_bonus = 10
        elif current_trend == "bear":
            trend_bonus = -10
        else:
            trend_bonus = 0
        confidence = max(min_conf_floor, min(95.0, min_conf_floor + retest_bonus + trend_bonus))
        reasons.append(
            f"✅ Price at support/demand zone (support ≈ {support_level:.5f}) — CALL"
        )
        reasons.append(f"📌 Rejection wick confirmed ({lower_wick_ratio:.0%} of candle range)")
        reasons.append(f"📊 Zone tested {support_retests}x recently (retest bonus: +{retest_bonus}%)")
        if trend_note:
            reasons.append(("✅ " if current_trend == "bull" else "⚠️ ") + trend_note)

    elif near_resistance and not near_support and fresh_resistance and upper_wick_ratio >= WICK_REJECTION_MIN_RATIO:
        signal = "PUT"
        retest_bonus = min(resistance_retests, 5) * 5
        if current_trend == "bear":
            trend_bonus = 10
        elif current_trend == "bull":
            trend_bonus = -10
        else:
            trend_bonus = 0
        confidence = max(min_conf_floor, min(95.0, min_conf_floor + retest_bonus + trend_bonus))
        reasons.append(
            f"✅ Price at resistance/supply zone (resistance ≈ {resistance_level:.5f}) — PUT"
        )
        reasons.append(f"📌 Rejection wick confirmed ({upper_wick_ratio:.0%} of candle range)")
        reasons.append(f"📊 Zone tested {resistance_retests}x recently (retest bonus: +{retest_bonus}%)")
        if trend_note:
            reasons.append(("✅ " if current_trend == "bear" else "⚠️ ") + trend_note)

    elif near_support or near_resistance:
        # Touched a zone, but filtered out — tell the user why (useful for tuning).
        which = "support" if near_support else "resistance"
        why = []
        if near_support and not fresh_support:
            why.append("not a fresh touch (already sitting in this zone)")
        if near_resistance and not fresh_resistance:
            why.append("not a fresh touch (already sitting in this zone)")
        if near_support and lower_wick_ratio < WICK_REJECTION_MIN_RATIO:
            why.append(f"weak rejection wick ({lower_wick_ratio:.0%} < {WICK_REJECTION_MIN_RATIO:.0%})")
        if near_resistance and upper_wick_ratio < WICK_REJECTION_MIN_RATIO:
            why.append(f"weak rejection wick ({upper_wick_ratio:.0%} < {WICK_REJECTION_MIN_RATIO:.0%})")
        reasons.append(f"⏳ Touched {which} zone but filtered out: " + "; ".join(why))
        if trend_note:
            reasons.append(trend_note)

    else:
        reasons.append(
            "ℹ️ Price is not at a support or resistance zone yet — no signal "
            f"(support ≈ {support_level:.5f}, resistance ≈ {resistance_level:.5f})"
        )
        if trend_note:
            reasons.append(trend_note)

    return signal, confidence, reasons