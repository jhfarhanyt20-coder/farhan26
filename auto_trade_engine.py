"""
auto_trade_engine.py
---------------------
Fully-automatic trading engine. Watches the pairs you select, waits for a
CALL/PUT signal from signal_logic.py, and places the trade itself — no
manual clicking.

Entry timing (both the first trade AND the 1-step martingale retry use the
exact same rule — press ENTRY_LEAD_SECONDS before the next candle opens so
network/API latency still lands the order on the entry candle):

    watch pair → signal appears → wait until ENTRY_LEAD_SECONDS before the
    NEXT candle boundary → place the trade → trade runs the full candle →
    result known right as that candle closes.

If the trade loses and martingale is enabled, exactly ONE retry is taken on
the very next candle, in the SAME direction as the losing trade, with the
stake multiplied by the martingale multiplier, using the same lead-time
timing again. Win or lose, the stake resets to base afterward (1-step only
— no further doubling).

engine.status        → "stopped" | "connecting" | "running" | "error" |
                        "target_hit" | "stop_loss_hit"
engine.status_detail  → human-readable current activity
engine.error          → last error string or None
engine.trades         → list of trade log dicts, newest last
engine.stats          → dict: total_profit, wins, losses, current_stake, balance
"""

import asyncio
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from quotexapi.stable_api import Quotex
from signal_logic import calculate_indicators, get_signal_simple, calculate_htf_trend, get_next_candle_window

logger = logging.getLogger(__name__)

USER_AGENT         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
CANDLE_OFFSET      = 3600 * 3
CONNECT_TIMEOUT    = 40
ENTRY_LEAD_SECONDS = 3.0  # press trade this many seconds before entry candle opens
IDLE_SCAN_SLEEP    = 2      # seconds between "no signal yet" pair checks
MAX_TRADES_LOG     = 300


def _runtime_dir() -> str:
    d = os.path.join(tempfile.gettempdir(), "quotex-streamlit-autotrade")
    os.makedirs(d, exist_ok=True)
    return d


class _AutoTradeEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self.status: str = "stopped"
        self.status_detail: str = ""
        self.error: Optional[str] = None
        self.trades: list = []
        self.stats: dict = {
            "total_profit": 0.0, "wins": 0, "losses": 0,
            "current_stake": 0.0, "balance": None,
        }
        self._config: dict = {}
        # local_clock + _server_offset == Quotex server clock (fixes local
        # PC/VPS clock drift, which was causing entries to fire off the
        # real candle boundary by several seconds).
        self._server_offset: float = 0.0

    # ── public API ───────────────────────────────────────────────────────────

    def start(self, credentials: dict, config: dict) -> None:
        self.stop()
        self._config = config
        self.trades = []
        self.stats = {
            "total_profit": 0.0, "wins": 0, "losses": 0,
            "current_stake": config.get("stake", 1.0), "balance": None,
            "server_offset_sec": 0.0,
        }
        self._server_offset = 0.0
        self._stop_evt.clear()
        self._set("connecting", "Starting auto-trade engine…")
        self._thread = threading.Thread(
            target=self._run, args=(credentials,), daemon=True, name="qx-autotrade"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=6)
        self._thread = None
        if self.status not in ("target_hit", "stop_loss_hit"):
            self._set("stopped", "")

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ── internal helpers ─────────────────────────────────────────────────────

    def _set(self, status: str, detail: str, error: Optional[str] = None) -> None:
        with self._lock:
            self.status = status
            self.status_detail = detail
            if error is not None:
                self.error = error

    def _log_trade(self, entry: dict) -> None:
        with self._lock:
            self.trades.append(entry)
            if len(self.trades) > MAX_TRADES_LOG:
                self.trades = self.trades[-MAX_TRADES_LOG:]

    def _update_stats(self, profit: float, won: bool, stake_for_next: float) -> None:
        with self._lock:
            self.stats["total_profit"] = round(self.stats["total_profit"] + profit, 2)
            self.stats["wins"]   += 1 if won else 0
            self.stats["losses"] += 0 if won else 1
            self.stats["current_stake"] = stake_for_next

    def _set_balance(self, bal) -> None:
        with self._lock:
            self.stats["balance"] = bal

    # ── thread entry point ───────────────────────────────────────────────────

    def _run(self, credentials: dict) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main(credentials))
        except Exception as exc:
            detail = traceback.format_exc(limit=4)
            self._set("error", "Auto-trade engine crashed — see error below.", str(exc) + "\n\n" + detail)
        finally:
            loop.close()

    # ── async main ────────────────────────────────────────────────────────────

    async def _async_main(self, credentials: dict) -> None:
        cookie = credentials.get("cookies", "").strip()
        token  = credentials.get("token", "").strip()
        email  = credentials.get("email", "trader@example.com").strip() or "trader@example.com"
        passwd = credentials.get("password", "password").strip() or "password"

        if not cookie or not token:
            self._set("error", "Missing QX_COOKIES / QX_TOKEN.",
                      "Go to Connection page and (re)paste your .env first.")
            return

        orig = os.getcwd()
        os.chdir(_runtime_dir())
        client = None
        try:
            self._set("connecting", "Building Quotex client…")
            try:
                client = Quotex(email=email, password=passwd, lang="en", user_agent=USER_AGENT)
                client.set_session(user_agent=USER_AGENT, cookies=cookie, ssid=token)
            except Exception as exc:
                self._set("error", f"Client init failed: {exc}", traceback.format_exc(limit=4))
                return

            self._set("connecting", f"Connecting to Quotex WebSocket… (timeout {CONNECT_TIMEOUT}s)")
            try:
                ok, reason = await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
            except asyncio.TimeoutError:
                self._set("error", f"Connection timed out after {CONNECT_TIMEOUT}s.",
                          "Cookies/token may be expired — re-paste from browser.")
                return
            except Exception as exc:
                self._set("error", f"Connection error: {exc}", traceback.format_exc(limit=6))
                return

            if not ok:
                self._set("error", f"Quotex rejected the connection: {reason or 'no reason given'}",
                          "Your cookies / token may be expired.")
                return

            # ── switch to the requested account (demo / real) ─────────────────
            account_mode = self._config.get("account_type", "PRACTICE").upper()
            if account_mode not in ("PRACTICE", "REAL"):
                account_mode = "PRACTICE"
            try:
                client.change_account(account_mode)
            except Exception as exc:
                self._set("error", f"Could not switch account: {exc}", traceback.format_exc(limit=4))
                return

            try:
                bal = await asyncio.wait_for(client.get_balance(), timeout=10)
                self._set_balance(bal)
            except Exception:
                pass

            self._set("running", f"Connected ✓ — auto-trading on {account_mode} account, watching pairs…")

            trade_task = asyncio.create_task(self._trade_loop(client))
            ka_task    = asyncio.create_task(self._keepalive_loop(client))

            done, pending = await asyncio.wait(
                [trade_task, ka_task], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            for t in done:
                exc = t.exception()
                if exc and self.status not in ("target_hit", "stop_loss_hit"):
                    self._set("error", str(exc), traceback.format_exc(limit=4))

        finally:
            os.chdir(orig)
            if client:
                try:
                    await asyncio.wait_for(client.close(), timeout=3)
                except Exception:
                    pass

    # ── keepalive ─────────────────────────────────────────────────────────────

    async def _keepalive_loop(self, client: Quotex) -> None:
        while not self._stop_evt.is_set():
            for _ in range(120):
                if self._stop_evt.is_set():
                    return
                await asyncio.sleep(0.25)
            try:
                ok = await asyncio.wait_for(Quotex.check_connect(), timeout=10)
                if not ok:
                    self._set("connecting", "Session lost — reconnecting…")
                    await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
                    self._set("running", "Reconnected ✓ — auto-trading resumed")
            except Exception:
                pass  # non-fatal; trade loop will surface real errors

    # ── sleep helper that still respects stop() ─────────────────────────────

    async def _sleep_until(self, target_ts: float) -> bool:
        """Sleeps until target_ts. Returns False early if stopped."""
        while True:
            if self._stop_evt.is_set():
                return False
            remaining = target_ts - time.time()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(0.25, remaining))

    # ── server clock sync (fixes entry-timing drift) ────────────────────────

    def _update_server_offset(self, client: Quotex) -> None:
        """
        Quotex pushes its own server timestamp on every candle response
        (api.timesync.server_timestamp). We diff that against our local
        clock the instant it arrives to get a fresh offset, then use
        local_time + offset as "Quotex time" everywhere we schedule an
        entry. This is what keeps entries locked to the real candle
        boundary even if the PC/VPS clock is off by several seconds.
        """
        try:
            srv_ts = client.api.timesync.server_timestamp
            if srv_ts:
                offset = float(srv_ts) - time.time()
                if abs(offset) < 3600:  # sanity guard against a bad reading
                    self._server_offset = offset
                    with self._lock:
                        self.stats["server_offset_sec"] = round(offset, 2)
        except Exception:
            pass

    def _server_now(self) -> float:
        return time.time() + self._server_offset

    # ── signal fetch ──────────────────────────────────────────────────────────

    async def _get_signal(self, client: Quotex, symbol: str, duration: int):
        candles = await asyncio.wait_for(
            client.get_candles(symbol, end_from_time=time.time(), offset=CANDLE_OFFSET, period=duration),
            timeout=15,
        )
        self._update_server_offset(client)
        if not candles or len(candles) < 30:
            return "neutral", 0.0, None

        df = pd.DataFrame(candles)
        htf_trend = "neutral"
        try:
            htf_raw = await asyncio.wait_for(
                client.get_candles(symbol, end_from_time=time.time(), offset=CANDLE_OFFSET,
                                    period=max(duration * 5, 300)),
                timeout=15,
            )
            if htf_raw:
                htf_trend = calculate_htf_trend(pd.DataFrame(htf_raw))
        except Exception:
            pass

        df = calculate_indicators(df)
        sig, conf, _ = get_signal_simple(df, htf_trend=htf_trend)
        direction = {"CALL": "call", "PUT": "put"}.get(sig, "neutral")
        last_close = df["Close"].iloc[-1]
        last_open  = df["Open"].iloc[-1]
        return direction, conf, (float(last_close), float(last_open))

    # ── place one trade + wait for its result ───────────────────────────────

    async def _place_and_wait(self, client: Quotex, symbol: str, direction: str,
                               stake: float, duration: int) -> tuple:
        """Returns (won: bool|None, profit: float, trade_id) — won=None on error."""
        try:
            status_buy, resp = await client.buy(stake, symbol, direction, duration)
        except Exception as exc:
            return None, 0.0, str(exc)

        if not status_buy or not resp:
            return None, 0.0, None

        trade_id = resp.get("id") if isinstance(resp, dict) else None
        if trade_id is None:
            return None, 0.0, None

        try:
            won = await asyncio.wait_for(client.check_win(trade_id), timeout=duration + 30)
        except Exception:
            return None, 0.0, trade_id

        profit = client.get_profit()
        return won, float(profit or 0.0), trade_id

    # ── one full opportunity cycle: entry + optional 1-step martingale ─────

    async def _run_cycle(self, client: Quotex, pair: dict, direction: str, duration: int) -> None:
        cfg        = self._config
        base_stake = float(cfg.get("stake", 1.0))
        mtg_on     = bool(cfg.get("martingale_enabled", False))
        mtg_mult   = float(cfg.get("martingale_multiplier", 2.0))
        symbol     = pair["symbol"]

        # ── wait until ENTRY_LEAD_SECONDS before the next candle, then fire ─
        # (boundary computed on QUOTEX's server clock, not the local PC/VPS
        # clock, so it matches the real candle on the QX chart)
        entry_dt, exit_dt, _ = get_next_candle_window(duration, now=self._server_now())
        self._set("running", f"Signal {direction.upper()} on {pair['displayName']} — "
                              f"waiting for entry window…")
        if not await self._sleep_until(entry_dt.timestamp() - ENTRY_LEAD_SECONDS):
            return

        self._set("running", f"Placing trade: {pair['displayName']} {direction.upper()} ${base_stake:.2f}")
        won, profit, trade_id = await self._place_and_wait(client, symbol, direction, base_stake, duration)
        self._record_and_check(pair, direction, base_stake, won, profit, is_mtg=False)

        if won is False and mtg_on and self.status == "running":
            # ── 1-step martingale retry on the very next candle, SAME direction ─
            mtg_stake = round(base_stake * mtg_mult, 2)
            entry_dt2, exit_dt2, _ = get_next_candle_window(duration, now=self._server_now())
            self._set("running", f"Loss — placing 1-step martingale (same direction) on "
                                  f"{pair['displayName']}…")
            if not await self._sleep_until(entry_dt2.timestamp() - ENTRY_LEAD_SECONDS):
                return

            mtg_direction = direction  # same direction as the losing trade
            self._set("running", f"Placing MTG trade: {pair['displayName']} "
                                  f"{mtg_direction.upper()} ${mtg_stake:.2f}")
            won2, profit2, _ = await self._place_and_wait(client, symbol, mtg_direction, mtg_stake, duration)
            self._record_and_check(pair, mtg_direction, mtg_stake, won2, profit2, is_mtg=True)

        # stake always resets to base after a cycle (1-step martingale only)
        with self._lock:
            self.stats["current_stake"] = base_stake

    def _record_and_check(self, pair, direction, stake, won, profit, is_mtg) -> None:
        result = "WIN" if won else ("LOSS" if won is False else "ERROR")
        self._log_trade({
            "time": datetime.now().strftime("%H:%M:%S"),
            "pair": pair["displayName"],
            "market": pair["market"],
            "direction": direction.upper(),
            "stake": stake,
            "result": result,
            "profit": round(profit, 2),
            "mtg": is_mtg,
        })
        if won is not None:
            self._update_stats(profit if won else -stake, won, stake)

        cfg = self._config
        target = float(cfg.get("profit_target", 0) or 0)
        sl     = float(cfg.get("stop_loss", 0) or 0)
        total  = self.stats["total_profit"]
        if target > 0 and total >= target:
            self._set("target_hit", f"🎯 Profit target reached (${total:.2f} ≥ ${target:.2f}) — auto-trade stopped.")
            self._stop_evt.set()
        elif sl > 0 and total <= -sl:
            self._set("stop_loss_hit", f"🛑 Stop-loss reached (${total:.2f} ≤ -${sl:.2f}) — auto-trade stopped.")
            self._stop_evt.set()

    # ── main trade loop — scans selected pairs round-robin ─────────────────

    async def _trade_loop(self, client: Quotex) -> None:
        cfg      = self._config
        pairs    = cfg.get("pairs", [])
        duration = int(cfg.get("duration", 60))

        if not pairs:
            self._set("error", "No pairs selected.", "Select at least one pair before starting auto-trade.")
            return

        idx = 0
        while not self._stop_evt.is_set():
            pair = pairs[idx % len(pairs)]
            idx += 1
            try:
                direction, conf, _ = await self._get_signal(client, pair["symbol"], duration)
            except Exception:
                direction = "neutral"

            if direction in ("call", "put") and self.status == "running":
                await self._run_cycle(client, pair, direction, duration)
                if self._stop_evt.is_set():
                    return
                self._set("running", "Watching pairs for the next signal…")
            else:
                if not await self._sleep_until(time.time() + IDLE_SCAN_SLEEP):
                    return


# ─── Singleton ────────────────────────────────────────────────────────────────

auto_engine = _AutoTradeEngine()
