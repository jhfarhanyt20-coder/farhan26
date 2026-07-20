"""
background_engine.py
---------------------
Persistent Quotex connection + scan loop in a daemon thread.
Shows detailed status at every step so the UI can surface real errors
instead of silently blinking "connecting".

engine.status        → "disconnected" | "connecting" | "connected" | "error"
engine.status_detail → human-readable string of what is happening right now
engine.error         → last error message (str) or None
engine.signals       → list of latest signal dicts (one per pair)
engine.last_scan_time→ float timestamp of last completed scan
"""

import asyncio
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pairs import all_pairs
from quotexapi.stable_api import Quotex
from signal_logic import calculate_indicators, get_signal_simple, calculate_htf_trend

logger = logging.getLogger(__name__)

USER_AGENT         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CANDLE_OFFSET      = 3600 * 3
POLL_INTERVAL      = 20       # seconds between full pair scans
KEEPALIVE_INTERVAL = 30       # seconds between keepalive pings
CONNECT_TIMEOUT    = 40       # seconds before giving up on connect
CANDLE_TIMEOUT     = 15       # seconds before giving up on one candle fetch
MAX_KA_FAILURES    = 5        # give up after N consecutive keepalive failures


def _runtime_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "quotex-streamlit-engine")
    os.makedirs(d, exist_ok=True)
    return d


# ─── Engine ───────────────────────────────────────────────────────────────────

class _Engine:
    def __init__(self):
        self._lock     = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self.status:        str           = "disconnected"
        self.status_detail: str           = ""
        self.error:         Optional[str] = None
        self.signals:       list          = []
        self.last_scan_time: Optional[float] = None
        self._credentials:  dict          = {}

    # ── public API ────────────────────────────────────────────────────────────

    def start(self, credentials: dict) -> None:
        self.stop()
        self._credentials = credentials
        self._stop_evt.clear()
        self._set("connecting", "Starting engine…")
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="qx-engine"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=6)
        self._thread = None
        self._set("disconnected", "")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── internal helpers ──────────────────────────────────────────────────────

    def _set(self, status: str, detail: str, error: str | None = None) -> None:
        with self._lock:
            self.status        = status
            self.status_detail = detail
            self.error         = error

    def _set_signals(self, sigs: list) -> None:
        with self._lock:
            self.signals         = sigs
            self.last_scan_time  = time.time()

    # ── thread entry point ────────────────────────────────────────────────────

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as exc:
            detail = traceback.format_exc(limit=4)
            self._set("error", "Engine crashed — see error below.", str(exc) + "\n\n" + detail)
        finally:
            loop.close()

    # ── async main ────────────────────────────────────────────────────────────

    async def _async_main(self) -> None:
        creds  = self._credentials
        cookie = creds.get("cookies", "").strip()
        token  = creds.get("token", "").strip()
        email  = creds.get("email", "trader@example.com").strip() or "trader@example.com"
        passwd = creds.get("password", "password").strip() or "password"

        # ── validate credentials ──────────────────────────────────────────────
        if not cookie:
            self._set("error", "QX_COOKIES is empty.",
                      "QX_COOKIES is empty.\n\nGo to Connection page and paste your .env file again.")
            return
        if not token:
            self._set("error", "QX_TOKEN is empty.",
                      "QX_TOKEN is empty.\n\nGo to Connection page and paste your .env file again.")
            return
        if len(cookie) < 20:
            self._set("error", "QX_COOKIES looks too short — may be wrong.",
                      f"QX_COOKIES value is only {len(cookie)} chars. "
                      "It should be a long browser cookie string.")
            return
        if len(token) < 10:
            self._set("error", "QX_TOKEN looks too short — may be wrong.",
                      f"QX_TOKEN value is only {len(token)} chars.")
            return

        orig = os.getcwd()
        os.chdir(_runtime_dir())
        client = None
        try:
            # ── build client ──────────────────────────────────────────────────
            self._set("connecting", "Building Quotex client…")
            try:
                client = Quotex(
                    email=email, password=passwd, lang="en",
                    user_agent=USER_AGENT,
                )
                client.set_session(
                    user_agent=USER_AGENT,
                    cookies=cookie,
                    ssid=token,
                )
            except Exception as exc:
                self._set("error", f"Client init failed: {exc}",
                          f"Failed to create Quotex client:\n{traceback.format_exc(limit=4)}")
                return

            # ── connect with timeout ──────────────────────────────────────────
            self._set("connecting",
                      f"Connecting to Quotex WebSocket… (timeout {CONNECT_TIMEOUT}s)")
            try:
                ok, reason = await asyncio.wait_for(
                    client.connect(), timeout=CONNECT_TIMEOUT
                )
            except asyncio.TimeoutError:
                self._set(
                    "error",
                    f"Connection timed out after {CONNECT_TIMEOUT}s.",
                    f"Could not reach Quotex after {CONNECT_TIMEOUT} seconds.\n\n"
                    "Possible causes:\n"
                    "• QX_COOKIES / QX_TOKEN are expired — re-paste from browser\n"
                    "• No internet connection\n"
                    "• Quotex servers are down"
                )
                return
            except Exception as exc:
                self._set("error", f"Connection error: {exc}",
                          f"Exception during connect():\n{traceback.format_exc(limit=6)}")
                return

            if not ok:
                self._set(
                    "error",
                    f"Quotex rejected the connection: {reason or 'no reason given'}",
                    f"connect() returned ok=False.\nReason: {reason}\n\n"
                    "Your cookies / token may be expired. Re-paste from browser."
                )
                return

            self._set("connected", "Connected ✓ — starting pair scans…")

            # ── run scan + keepalive ──────────────────────────────────────────
            scan_task = asyncio.create_task(self._scan_loop(client))
            ka_task   = asyncio.create_task(self._keepalive_loop(client))

            done, pending = await asyncio.wait(
                [scan_task, ka_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc:
                    self._set("error", str(exc),
                              traceback.format_exc(limit=4))

        finally:
            os.chdir(orig)
            if client:
                try:
                    await asyncio.wait_for(client.close(), timeout=3)
                except Exception:
                    pass

    # ── scan loop ─────────────────────────────────────────────────────────────

    async def _scan_loop(self, client: Quotex) -> None:
        pairs   = all_pairs()
        scan_no = 0
        while not self._stop_evt.is_set():
            scan_no += 1
            results  = []
            errors   = []
            for i, p in enumerate(pairs, 1):
                if self._stop_evt.is_set():
                    return
                self._set("connected",
                          f"Scan #{scan_no} — pair {i}/{len(pairs)}: {p['displayName']}")
                sig = await self._fetch_signal(
                    client, p["symbol"], p["displayName"], p["market"]
                )
                results.append(sig)
                if sig.get("error"):
                    errors.append(f"{p['displayName']}: {sig['error']}")

            self._set_signals(results)
            err_summary = f"  ({len(errors)} pairs had errors)" if errors else ""
            self._set("connected",
                      f"Scan #{scan_no} done{err_summary} — next scan in {POLL_INTERVAL}s")

            # sleep in small chunks so stop_evt is checked quickly
            for _ in range(POLL_INTERVAL * 4):
                if self._stop_evt.is_set():
                    return
                await asyncio.sleep(0.25)

    # ── keepalive loop ────────────────────────────────────────────────────────

    async def _keepalive_loop(self, client: Quotex) -> None:
        failures = 0
        while not self._stop_evt.is_set():
            for _ in range(KEEPALIVE_INTERVAL * 4):
                if self._stop_evt.is_set():
                    return
                await asyncio.sleep(0.25)
            try:
                ok = await asyncio.wait_for(
                    Quotex.check_connect(), timeout=10
                )
                if not ok:
                    self._set("connecting", "Session lost — reconnecting…")
                    await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
                    self._set("connected", "Reconnected ✓")
                failures = 0
            except Exception as exc:
                failures += 1
                if failures >= MAX_KA_FAILURES:
                    raise RuntimeError(
                        f"Keepalive failed {failures} times in a row. "
                        f"Last error: {exc}\n\n"
                        "Your Quotex session has probably expired. "
                        "Go to Connection page and re-paste fresh cookies."
                    )

    # ── fetch one signal ──────────────────────────────────────────────────────

    async def _fetch_signal(
        self, client: Quotex, symbol: str, display_name: str,
        market: str, period: int = 60
    ) -> dict:
        try:
            candles = await asyncio.wait_for(
                client.get_candles(
                    symbol,
                    end_from_time=time.time(),
                    offset=CANDLE_OFFSET,
                    period=period,
                ),
                timeout=CANDLE_TIMEOUT,
            )
            if not candles or len(candles) < 30:
                s = self._empty(symbol, display_name, market)
                s["error"] = "Not enough candle data (< 30 candles)"
                return s

            df = pd.DataFrame(candles)

            htf_trend = "neutral"
            try:
                htf_raw = await asyncio.wait_for(
                    client.get_candles(
                        symbol,
                        end_from_time=time.time(),
                        offset=CANDLE_OFFSET,
                        period=max(period * 5, 300),
                    ),
                    timeout=CANDLE_TIMEOUT,
                )
                if htf_raw:
                    htf_trend = calculate_htf_trend(pd.DataFrame(htf_raw))
            except Exception:
                pass  # HTF is best-effort; carry on with neutral trend

            df = calculate_indicators(df)
            sig, conf, reasons = get_signal_simple(df, htf_trend=htf_trend)
            direction  = {"CALL": "call", "PUT": "put"}.get(sig, "neutral")
            last_close = df["Close"].iloc[-1]
            price      = None if last_close != last_close else round(float(last_close), 5)

            return {
                "symbol":      symbol,
                "displayName": display_name,
                "market":      market,
                "direction":   direction,
                "confidence":  conf,
                "price":       price,
                "reasons":     reasons[-6:],
                "timestamp":   time.time(),
            }

        except asyncio.TimeoutError:
            s = self._empty(symbol, display_name, market)
            s["error"] = f"Timed out fetching candles (>{CANDLE_TIMEOUT}s)"
            return s
        except Exception as exc:
            s = self._empty(symbol, display_name, market)
            s["error"] = str(exc)
            return s

    @staticmethod
    def _empty(symbol, display_name, market) -> dict:
        return {
            "symbol":      symbol,
            "displayName": display_name,
            "market":      market,
            "direction":   "neutral",
            "confidence":  0,
            "price":       None,
            "reasons":     [],
            "timestamp":   time.time(),
        }


# ─── Singleton ────────────────────────────────────────────────────────────────

engine = _Engine()
