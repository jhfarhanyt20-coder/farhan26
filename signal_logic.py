"""
signal_logic.py
================
UT Bot Alerts (Pine Script v6 "UT Bot Alerts + Dashboard v2") — hubohu Python port.

Public API kept IDENTICAL to the previous multi-indicator version so that
main.py / bot code does NOT need any changes:

    get_next_candle_window(period_seconds, now=None)
    get_value_safe(series, index=-1, default=0.0)
    calculate_indicators(df) -> df
    calculate_htf_trend(df) -> "bull" | "bear" | "neutral"
    get_signal_simple(df, htf_trend="neutral", min_confidence=70.0, df_5m=None)
        -> (signal: "CALL"/"PUT"/None, confidence: float, reasons: list[str])

Only the INTERNAL logic changed — it now mirrors the UT Bot Pine indicator
line-by-line instead of the old RSI/EMA/BB/Stoch/CCI compound-scoring engine.
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
# Forming-candle guard
# ============================================================

def drop_forming_candle(df: pd.DataFrame, period: int, now: float = None) -> pd.DataFrame:
    """Some broker feeds (e.g. history/list/v2) push the still-forming,
    not-yet-closed candle as the last row of the candle list. Feeding that
    into an indicator that relies on confirmed closes (like UT Bot's
    crossover/trailing-stop logic) causes the last signal to repaint as the
    candle keeps moving. Call this right after fetching candles — for both
    the entry-timeframe df and any higher-timeframe df_5m — before passing
    them into calculate_indicators()/get_signal_simple().

    Safe no-op if df is empty or has no usable "time" column.
    """
    import time as _time

    if df is None or len(df) == 0:
        return df

    time_col = None
    for cand in ("time", "Time", "timestamp", "Timestamp"):
        if cand in df.columns:
            time_col = cand
            break
    if time_col is None:
        # No timestamp to check against — can't safely tell, leave as-is.
        return df

    now = now or _time.time()
    try:
        last_time = float(df[time_col].iloc[-1])
    except Exception:
        return df

    if (last_time + period) > now:
        return df.iloc[:-1].reset_index(drop=True)
    return df


# ============================================================
# UT Bot settings — mirrors the Pine `input.*` lines exactly
# ============================================================

UT_KEY_VALUE   = 1     # a  -> Key Value (Sensitivity)
UT_ATR_PERIOD  = 14        # c  -> ATR Period
UT_USE_HEIKIN  = False     # h  -> Signals from Heikin Ashi Candles

USE_TREND_FILTER = True   # useTrendFilter
TREND_EMA_LEN     = 24     # trendEmaLen
TREND_TF_MINUTES  = 5      # trendTF = "5"  -> supplied via df_5m

USE_VOL_FILTER = False      # useVolFilter
VOL_MA_LEN      = 20        # volMaLen


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


def _atr_rma(df: pd.DataFrame, period: int) -> pd.Series:
    """Pine's ta.atr() = RMA (Wilder smoothing) of True Range."""
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def _heikin_ashi_close(df: pd.DataFrame) -> pd.Series:
    return (df["Open"] + df["High"] + df["Low"] + df["Close"]) / 4


# ============================================================
# Indicator calculations — builds everything the UT Bot needs
# ============================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = _normalize_ohlc(df)

    src = _heikin_ashi_close(df) if UT_USE_HEIKIN else df["Close"]
    df["UT_Src"] = src

    xATR = _atr_rma(df, UT_ATR_PERIOD)
    nLoss = (UT_KEY_VALUE * xATR).fillna(0.0)

    src_vals = src.values
    nloss_vals = nLoss.values
    n = len(df)
    stop = np.zeros(n)

    # ---- xATRTrailingStop, computed exactly like the Pine recursive formula ----
    for i in range(n):
        if i == 0:
            stop[i] = src_vals[i] - nloss_vals[i]
            continue
        prev_stop = stop[i - 1]
        prev_src = src_vals[i - 1]
        cur_src = src_vals[i]
        nloss = nloss_vals[i]

        if cur_src > prev_stop and prev_src > prev_stop:
            stop[i] = max(prev_stop, cur_src - nloss)
        elif cur_src < prev_stop and prev_src < prev_stop:
            stop[i] = min(prev_stop, cur_src + nloss)
        elif cur_src > prev_stop:
            stop[i] = cur_src - nloss
        else:
            stop[i] = cur_src + nloss

    df["UT_TrailingStop"] = stop

    # ema = ta.ema(src, 1)  -> a length-1 EMA is just the source value itself
    ema = src
    trail = df["UT_TrailingStop"]

    above = (ema > trail) & (ema.shift(1) <= trail.shift(1))   # ta.crossover(ema, stop)
    below = (trail > ema) & (trail.shift(1) <= ema.shift(1))   # ta.crossover(stop, ema)

    df["UT_RawBuy"] = (src > trail) & above
    df["UT_RawSell"] = (src < trail) & below

    df["UT_VolMA"] = df["Volume"].rolling(VOL_MA_LEN).mean()

    return df


def calculate_htf_trend(df: pd.DataFrame) -> str:
    """
    Kept for backward compatibility (in case main.py calls this directly).
    UT Bot's own trend filter uses df_5m inside get_signal_simple instead.
    """
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
    """trendEma = request.security(..., trendTF='5', ta.ema(close, trendEmaLen))"""
    if df_5m is None or len(df_5m) < length:
        return None
    d = _normalize_ohlc(df_5m)
    ema = d["Close"].ewm(span=length, adjust=False).mean()
    return get_value_safe(ema)


# ============================================================
# Main signal generator — UT Bot Alerts (hubohu Pine logic)
# ============================================================

def get_signal_simple(df: pd.DataFrame, htf_trend: str = "neutral", min_confidence: float = 70.0, df_5m: pd.DataFrame = None):
    """
    Signature unchanged from the old version so the bot's call site
    (signal, confidence, reasons = get_signal_simple(...)) keeps working.

    UT Bot alerts are binary (not scored), so confidence is 100 on a
    valid buy/sell trigger and 0 otherwise. min_confidence is accepted
    for compatibility but has no effect on this logic (100 always clears it).
    """
    reasons = []

    if df is None or len(df) < 30:
        return None, 0, ["❌ Not enough candle data (need at least 30)"]

    df = calculate_indicators(df)

    close = get_value_safe(df["Close"])
    src = get_value_safe(df["UT_Src"])
    trail = get_value_safe(df["UT_TrailingStop"])
    raw_buy = bool(df["UT_RawBuy"].iloc[-1])
    raw_sell = bool(df["UT_RawSell"].iloc[-1])
    volume = get_value_safe(df["Volume"])
    vol_ma = get_value_safe(df["UT_VolMA"])

    # ---- Trend filter: 5-minute EMA(24), matches trendTF="5" ----
    uptrend = True
    downtrend = True
    if USE_TREND_FILTER:
        trend_ema = _trend_ema_from_5m(df_5m, TREND_EMA_LEN)
        if trend_ema is None:
            # No 5m data supplied -> fall back to caller-provided htf_trend string
            uptrend = htf_trend == "bull"
            downtrend = htf_trend == "bear"
            reasons.append("⚠️ df_5m na thakay htf_trend string diye trend filter kora hoyeche")
        else:
            uptrend = close > trend_ema
            downtrend = close < trend_ema
            reasons.append(f"Trend EMA({TREND_EMA_LEN}, 5m) = {trend_ema:.5f}")

    # ---- Volume filter (off by default, matches useVolFilter=false) ----
    vol_ok = True
    if USE_VOL_FILTER:
        vol_ok = volume > vol_ma

    buy = raw_buy and (not USE_TREND_FILTER or uptrend) and vol_ok
    sell = raw_sell and (not USE_TREND_FILTER or downtrend) and vol_ok

    if buy:
        reasons.append(f"🟢 UT Bot BUY: src({src:.5f}) crossed above trailing stop({trail:.5f})")
        if USE_TREND_FILTER:
            reasons.append("✅ 5m uptrend diye confirmed")
        return "CALL", 100.0, reasons

    if sell:
        reasons.append(f"🔴 UT Bot SELL: trailing stop({trail:.5f}) crossed above src({src:.5f})")
        if USE_TREND_FILTER:
            reasons.append("✅ 5m downtrend diye confirmed")
        return "PUT", 100.0, reasons

    reasons.append(f"ℹ️ Ei candle-e UT Bot crossover hoyni (src={src:.5f}, stop={trail:.5f})")
    return None, 0.0, reasons