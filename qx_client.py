"""
qx_client.py
On-demand single-signal generator (used by the Generate Signal page).
Opens a fresh connection, fetches one pair, closes — independent of the
background engine so it works even when the engine is not running.
"""

import asyncio
import os
import sys
import tempfile
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quotexapi.stable_api import Quotex
from signal_logic import calculate_indicators, get_signal_simple, drop_forming_candle

USER_AGENT    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CANDLE_OFFSET = 3600 * 3


def _runtime_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "quotex-streamlit")
    os.makedirs(d, exist_ok=True)
    return d


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _do_generate(credentials: dict, symbol: str, display_name: str, market: str, period: int):
    orig = os.getcwd()
    os.chdir(_runtime_dir())
    try:
        client = Quotex(
            email=credentials.get("email", "user@example.com"),
            password=credentials.get("password", "password"),
            lang="en",
            user_agent=USER_AGENT,
        )
        client.set_session(
            user_agent=USER_AGENT,
            cookies=credentials.get("cookies", ""),
            ssid=credentials.get("token", ""),
        )
        ok, reason = await client.connect()
        if not ok:
            return None, str(reason) or "Connection failed"

        try:
            candles = await client.get_candles(
                symbol, end_from_time=time.time(), offset=CANDLE_OFFSET, period=period
            )
            if not candles or len(candles) < 30:
                return {
                    "symbol": symbol, "displayName": display_name, "market": market,
                    "direction": "neutral", "confidence": 0, "price": None,
                    "reasons": ["Not enough candle data"], "timestamp": time.time(),
                }, None

            df = pd.DataFrame(candles)
            # Broker sometimes pushes the still-forming (not yet closed) candle
            # as the last row — drop it so signals only ever fire on confirmed data.
            df = drop_forming_candle(df, period)
            if df is None or len(df) < 30:
                return {
                    "symbol": symbol, "displayName": display_name, "market": market,
                    "direction": "neutral", "confidence": 0, "price": None,
                    "reasons": ["Not enough confirmed candle data after dropping forming candle"],
                    "timestamp": time.time(),
                }, None

            df_5m = None
            try:
                htf_period = max(period * 5, 300)
                htf_raw = await client.get_candles(
                    symbol, end_from_time=time.time(), offset=CANDLE_OFFSET,
                    period=htf_period,
                )
                if htf_raw:
                    df_5m = pd.DataFrame(htf_raw)
                    df_5m = drop_forming_candle(df_5m, htf_period)
            except Exception:
                pass

            df    = calculate_indicators(df)
            sig, conf, reasons = get_signal_simple(df, df_5m=df_5m)
            direction = {"CALL": "call", "PUT": "put"}.get(sig, "neutral")
            last_close = df["Close"].iloc[-1]
            price = None if last_close != last_close else round(float(last_close), 5)

            return {
                "symbol": symbol, "displayName": display_name, "market": market,
                "direction": direction, "confidence": conf, "price": price,
                "reasons": reasons[-6:], "timestamp": time.time(),
            }, None

        finally:
            try:
                await client.close()
            except Exception:
                pass
    except Exception as exc:
        return None, str(exc)
    finally:
        os.chdir(orig)


def generate_signal_once(
    credentials: dict,
    symbol: str,
    display_name: str,
    market: str,
    duration_seconds: int = 60,
) -> tuple:
    """Blocking call. Returns (signal_dict, error_str). error_str is None on success."""
    return _run(_do_generate(credentials, symbol, display_name, market, duration_seconds))
