"""
sniper.py — pump.fun launch sniper engine.

Detects new pump.fun token launches via helius_ws.py and immediately buys
small positions, then auto-sells based on take-profit / stop-loss / time rules.

Intelligence features (all configurable per-uid):
  - Narrative pre-filter: skip tokens with no matching narrative keywords
  - Launch predictor: use archetype confidence to gate/size trades
  - Lifecycle cross-reference: skip low-score tokens the scanner already rated
  - Dev wallet bundle check: skip high bundle-risk launches
  - Time-of-day gating: only snipe during configured UTC hours
  - Adaptive position sizing: multipliers for narrative/predictor confidence
  - Telegram notifications: buy/sell alerts sent to the user

Architecture:
  helius_ws._handle_launch()
      └── sniper_engine.attempt_snipe(mint)
              └── pumpfun.buy_pumpfun()
                      └── _auto_sell_loop() polls every POLL_INTERVAL_SECS
                              └── pumpfun.sell_pumpfun()

Per-uid config and all positions/history are persisted in SQLite via db.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import requests
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import db as _db
import config as _cfg
from pumpfun import (
    buy_pumpfun,
    sell_pumpfun,
    fetch_bonding_curve_data,
    calculate_buy_tokens,
    get_token_balance,
    get_sol_balance,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECS = 5       # how often to check open positions
_KEYPAIR_CACHE: dict = {}    # module-level keypair cache
ZERO_TOKEN_CB_THRESHOLD = 3
ZERO_TOKEN_CB_WINDOW_SECS = 15 * 60
_BALANCE_RETRY_ATTEMPTS = 3
_BALANCE_RETRY_DELAY = 1.5   # seconds between retries
_MAX_SELL_CYCLES = 5          # force-close position after this many failed sell cycles


async def _get_token_balance_reliable(
    owner: str,
    mint: str,
    rpc: str,
    loop: asyncio.AbstractEventLoop,
    *,
    attempts: int = _BALANCE_RETRY_ATTEMPTS,
    delay: float = _BALANCE_RETRY_DELAY,
) -> int | None:
    """Retry-aware wrapper around get_token_balance.

    Returns:
        int  — confirmed balance (may be 0)
        None — all attempts returned RPC errors (balance unknown)
    """
    for attempt in range(1, attempts + 1):
        result = await loop.run_in_executor(
            None, get_token_balance, owner, mint, rpc
        )
        if result is not None:
            return result
        if attempt < attempts:
            logger.warning(
                f"[Sniper] get_token_balance RPC error for {mint[:8]} "
                f"(attempt {attempt}/{attempts}) — retrying in {delay}s"
            )
            await asyncio.sleep(delay)
    logger.error(
        f"[Sniper] get_token_balance failed all {attempts} attempts for {mint[:8]}"
    )
    return None

# ── Narrative keywords (mirrors AGENTS.md) ────────────────────────────────────

NARRATIVE_KEYWORDS: dict[str, list[str]] = {
    "AI":       ["ai", "agent", "gpt", "robot", "artificial", "neural", "agi", "llm",
                 "compute", "singularity"],
    "Political":["trump", "maga", "biden", "elon", "doge", "political", "president",
                 "potus", "vote", "election", "america", "patriot"],
    "Animal":   ["dog", "cat", "pepe", "frog", "shib", "inu", "pup", "kitty", "bunny",
                 "bear", "bull", "whale", "degen", "hamster", "goat", "duck"],
    "Gaming":   ["game", "play", "nft", "pixel", "arcade", "quest", "hero", "rpg",
                 "guild", "farm", "casino"],
    "RWA":      ["gold", "silver", "real", "estate", "asset", "bond", "stock",
                 "nasdaq", "sp500"],
}


def _detect_narrative(name: str, symbol: str) -> tuple[bool, str]:
    """
    Check if token name/symbol matches a known narrative.
    Returns (matched: bool, narrative_name: str).
    narrative_name is "Other" when no match found.
    """
    text = f"{name} {symbol}".lower()
    for narrative, keywords in NARRATIVE_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return True, narrative
    return False, "Other"


# ── Notification bot reference ────────────────────────────────────────────────

_notify_bot = None   # telegram Bot instance, set by set_notify_bot()


def set_notify_bot(bot):
    """Called from bot.py post_init to give the sniper a handle to send messages."""
    global _notify_bot
    _notify_bot = bot


async def _send_notify(uid: int, text: str):
    """Fire-and-forget Telegram message to uid. Silently drops on error."""
    if _notify_bot is None:
        return
    try:
        await _notify_bot.send_message(uid, text, parse_mode="Markdown")
    except Exception as e:
        logger.debug(f"[Sniper] notify failed uid={uid}: {e}")


# ── User mode helper ──────────────────────────────────────────────────────────

_MODES_FILE = os.path.join(os.path.dirname(__file__), "data", "user_modes.json")


def _get_user_mode(uid: int) -> str:
    """Return 'paper' or 'live' for the given uid. Reads from user_modes.json
    (same file bot.py uses) to avoid circular imports."""
    try:
        with open(_MODES_FILE) as f:
            modes = json.load(f)
        return modes.get(str(uid), "paper")
    except (FileNotFoundError, json.JSONDecodeError):
        return "paper"


# ── Keypair helper ────────────────────────────────────────────────────────────

def _get_keypair():
    """Return the trading Keypair, cached after first load."""
    if _KEYPAIR_CACHE.get("kp"):
        return _KEYPAIR_CACHE["kp"]
    from solders.keypair import Keypair
    key = getattr(_cfg, "WALLET_PRIVATE_KEY", "")
    if not key:
        raise RuntimeError("WALLET_PRIVATE_KEY not set in config")
    kp = Keypair.from_base58_string(key)
    _KEYPAIR_CACHE["kp"] = kp
    return kp


def _is_retryable_pumpfun_buy_error(result: str) -> bool:
    """Retry fresh-launch pump.fun failures that usually resolve with indexing delay."""
    if not isinstance(result, str):
        return False
    return (
        result.startswith("ERROR: Bonding curve not found")
        or "ProgramAccountNotFound" in result
        or "Attempt to load a program that does not exist" in result
    )


async def _retry_live_buy_on_curve_race(
    mint: str,
    trade_sol: float,
    kp,
    rpc_url: str,
    loop: asyncio.AbstractEventLoop,
    *,
    attempts: int = 5,
    delay_secs: float = 2.0,
) -> str:
    """
    Retry very fresh pump.fun buys when RPC has not indexed the bonding curve yet.
    Only retries the specific transient "Bonding curve not found" failure.
    """
    last_sig = ""
    for attempt in range(1, attempts + 1):
        sig = await loop.run_in_executor(
            None,
            lambda: buy_pumpfun(
                mint,
                trade_sol,
                kp,
                rpc_url,
                skip_preflight=False,
                max_retries=5,
            ),
        )
        last_sig = sig
        if not _is_retryable_pumpfun_buy_error(sig):
            return sig
        if attempt < attempts:
            logger.info(
                f"[Sniper] {mint[:8]} pump.fun not ready on attempt {attempt}/{attempts} — retrying in {delay_secs:.1f}s"
            )
            await asyncio.sleep(delay_secs)
    return last_sig


async def _poll_tx_confirmation(
    tx_sig: str,
    rpc_url: str,
    *,
    max_polls: int = 45,
    poll_interval: float = 1.0,
) -> str:
    """
    Wait for a broadcast tx to land on-chain before treating it as a real buy.

    Returns:
        "confirmed" — tx landed and succeeded
        "failed"    — tx landed but failed on-chain
        "timeout"   — tx never appeared within the poll window
    """
    rpc_candidates = []
    seen = set()
    for candidate in [rpc_url, *getattr(_cfg, "SOLANA_RPC_BACKUPS", [])]:
        candidate = str(candidate or "").strip()
        if candidate and candidate not in seen:
            rpc_candidates.append(candidate)
            seen.add(candidate)

    loop = asyncio.get_running_loop()
    polls_done = 0
    while polls_done < max_polls:
        saw_rate_limit = False
        for candidate_rpc in rpc_candidates:
            try:
                resp = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        lambda rpc=candidate_rpc: requests.post(
                            rpc,
                            json={
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "getTransaction",
                                "params": [
                                    tx_sig,
                                    {
                                        "encoding": "json",
                                        "commitment": "confirmed",
                                        "maxSupportedTransactionVersion": 0,
                                    },
                                ],
                            },
                            timeout=5,
                        ).json(),
                    ),
                    timeout=6,
                )
                if "error" in resp:
                    err = resp.get("error", {})
                    if isinstance(err, dict) and err.get("code") == 429:
                        saw_rate_limit = True
                    continue
                tx = resp.get("result")
                if tx is not None:
                    meta = tx.get("meta") or {}
                    return "failed" if meta.get("err") else "confirmed"
            except Exception:
                continue
        if saw_rate_limit:
            await asyncio.sleep(poll_interval)
        polls_done += 1
        await asyncio.sleep(poll_interval)
    return "timeout"


# ── Config / Position dataclasses ─────────────────────────────────────────────

@dataclass
class SniperConfig:
    uid: int
    # ── Core trade settings ───────────────────────────────────────────────────
    enabled: bool = False
    sol_amount: float = 0.05
    max_concurrent: int = 3
    take_profit_pct: float = 100.0   # sell when price is up X%
    stop_loss_pct: float = 30.0      # sell when price is down X%
    max_age_secs: int = 300          # force-close after N seconds
    dev_buy_max_pct: float = 10.0    # skip if dev holds > X% supply
    # ── Intelligence filters ──────────────────────────────────────────────────
    require_narrative: bool = False       # skip if name/symbol has no narrative match
    min_predictor_confidence: float = 0.0 # skip if predictor confidence < X (0=off)
    use_lifecycle_filter: bool = False    # cross-ref lifecycle store score
    max_bundle_risk: int = 10            # skip if bundle risk > X (10=disabled)
    # ── Adaptive sizing ───────────────────────────────────────────────────────
    sol_multiplier_narrative: float = 1.5  # multiply sol_amount when narrative matched
    sol_multiplier_predictor: float = 2.0  # multiply sol_amount when confidence >= 70
    # ── Scheduling ────────────────────────────────────────────────────────────
    active_hours_utc: str = ""   # "HH-HH" e.g. "12-23"; empty = all hours
    # ── Notifications ─────────────────────────────────────────────────────────
    telegram_notify: bool = True


@dataclass
class SniperPosition:
    uid: int
    mint: str
    symbol: str
    name: str
    tokens_bought: float
    sol_spent: float
    buy_price_sol: float             # SOL per token at entry
    buy_time: float
    mode: str = "paper"              # "paper" or "live"
    db_id: int = 0


# ── Database helpers ──────────────────────────────────────────────────────────

def _get_config(uid: int) -> SniperConfig:
    row = _db._fetchone(
        "SELECT * FROM sniper_config WHERE uid = ?", (uid,)
    )
    if not row:
        return SniperConfig(uid=uid)
    return SniperConfig(
        uid=uid,
        enabled=bool(row["enabled"]),
        sol_amount=row["sol_amount"],
        max_concurrent=row["max_concurrent"],
        take_profit_pct=row["take_profit_pct"],
        stop_loss_pct=row["stop_loss_pct"],
        max_age_secs=row["max_age_secs"],
        dev_buy_max_pct=row["dev_buy_max_pct"],
        require_narrative=bool(row["require_narrative"]),
        min_predictor_confidence=row["min_predictor_confidence"],
        use_lifecycle_filter=bool(row["use_lifecycle_filter"]),
        max_bundle_risk=row["max_bundle_risk"],
        sol_multiplier_narrative=row["sol_multiplier_narrative"],
        sol_multiplier_predictor=row["sol_multiplier_predictor"],
        active_hours_utc=row["active_hours_utc"] or "",
        telegram_notify=bool(row["telegram_notify"]),
    )


def _save_config(cfg: SniperConfig):
    _db._exec("""
        INSERT INTO sniper_config
            (uid, enabled, sol_amount, max_concurrent,
             take_profit_pct, stop_loss_pct, max_age_secs, dev_buy_max_pct,
             require_narrative, min_predictor_confidence, use_lifecycle_filter,
             max_bundle_risk, sol_multiplier_narrative, sol_multiplier_predictor,
             active_hours_utc, telegram_notify)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET
            enabled                    = excluded.enabled,
            sol_amount                 = excluded.sol_amount,
            max_concurrent             = excluded.max_concurrent,
            take_profit_pct            = excluded.take_profit_pct,
            stop_loss_pct              = excluded.stop_loss_pct,
            max_age_secs               = excluded.max_age_secs,
            dev_buy_max_pct            = excluded.dev_buy_max_pct,
            require_narrative          = excluded.require_narrative,
            min_predictor_confidence   = excluded.min_predictor_confidence,
            use_lifecycle_filter       = excluded.use_lifecycle_filter,
            max_bundle_risk            = excluded.max_bundle_risk,
            sol_multiplier_narrative   = excluded.sol_multiplier_narrative,
            sol_multiplier_predictor   = excluded.sol_multiplier_predictor,
            active_hours_utc           = excluded.active_hours_utc,
            telegram_notify            = excluded.telegram_notify
    """, (
        cfg.uid, int(cfg.enabled), cfg.sol_amount, cfg.max_concurrent,
        cfg.take_profit_pct, cfg.stop_loss_pct, cfg.max_age_secs, cfg.dev_buy_max_pct,
        int(cfg.require_narrative), cfg.min_predictor_confidence,
        int(cfg.use_lifecycle_filter), cfg.max_bundle_risk,
        cfg.sol_multiplier_narrative, cfg.sol_multiplier_predictor,
        cfg.active_hours_utc, int(cfg.telegram_notify),
    ))


def _open_position_count(uid: int) -> int:
    row = _db._fetchone(
        "SELECT COUNT(*) AS n FROM sniper_positions WHERE uid = ? AND status = 'open'",
        (uid,),
    )
    return row["n"] if row else 0


def _already_sniped(uid: int, mint: str) -> bool:
    row = _db._fetchone(
        "SELECT 1 FROM sniper_positions WHERE uid = ? AND mint = ?", (uid, mint)
    )
    return row is not None


def _insert_position(pos: SniperPosition) -> int:
    cur = _db._exec("""
        INSERT OR IGNORE INTO sniper_positions
            (uid, mint, symbol, name, tokens_bought, sol_spent,
             buy_price_sol, buy_time, status, mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)
    """, (
        pos.uid, pos.mint, pos.symbol, pos.name,
        pos.tokens_bought, pos.sol_spent, pos.buy_price_sol, pos.buy_time,
        pos.mode,
    ))
    return cur.lastrowid or 0


def _close_position_db(uid: int, mint: str, sol_received: float, exit_reason: str):
    """Move a position from sniper_positions to sniper_history."""
    row = _db._fetchone(
        "SELECT * FROM sniper_positions WHERE uid = ? AND mint = ? AND status = 'open'",
        (uid, mint),
    )
    if not row:
        return
    sell_time = time.time()
    hold_secs = sell_time - row["buy_time"]
    profit_sol = sol_received - row["sol_spent"]
    _db._exec(
        "UPDATE sniper_positions SET status = 'closed' WHERE uid = ? AND mint = ?",
        (uid, mint),
    )
    pos_mode = row["mode"] if "mode" in row.keys() else "paper"
    _db._exec("""
        INSERT INTO sniper_history
            (uid, mint, symbol, name, sol_spent, sol_received, profit_sol,
             buy_time, sell_time, hold_secs, exit_reason, mode)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        uid, mint, row["symbol"], row["name"],
        row["sol_spent"], sol_received, profit_sol,
        row["buy_time"], sell_time, hold_secs, exit_reason, pos_mode,
    ))


def _load_open_positions(uid: int) -> list[SniperPosition]:
    rows = _db._fetchall(
        "SELECT * FROM sniper_positions WHERE uid = ? AND status = 'open'", (uid,)
    )
    return [
        SniperPosition(
            uid=uid,
            mint=r["mint"],
            symbol=r["symbol"] or "",
            name=r["name"] or "",
            tokens_bought=r["tokens_bought"],
            sol_spent=r["sol_spent"],
            buy_price_sol=r["buy_price_sol"],
            buy_time=r["buy_time"],
            mode=r["mode"] if "mode" in r.keys() else "paper",
            db_id=r["id"],
        )
        for r in rows
    ]


# ── Intelligence helpers ──────────────────────────────────────────────────────

def _check_active_hours(active_hours_utc: str) -> bool:
    """
    Return True if current UTC hour is within the configured window.
    active_hours_utc format: "HH-HH" e.g. "12-23".  Empty string = always active.
    Handles wrap-around e.g. "22-6" (10pm–6am).
    """
    if not active_hours_utc or "-" not in active_hours_utc:
        return True
    try:
        start_str, end_str = active_hours_utc.split("-", 1)
        start_h = int(start_str.strip())
        end_h   = int(end_str.strip())
    except ValueError:
        return True
    now_h = datetime.now(timezone.utc).hour
    if start_h <= end_h:
        return start_h <= now_h <= end_h
    else:  # wrap-around (e.g. 22-6)
        return now_h >= start_h or now_h <= end_h


def _lifecycle_score(mint: str) -> Optional[float]:
    """
    Return the scanner's last_score for this mint from the lifecycle store.
    Returns None if the token has never been scored.
    """
    try:
        row = _db._fetchone(
            "SELECT last_score FROM token_lifecycle WHERE mint = ?", (mint,)
        )
        if row and row["last_score"] is not None:
            return float(row["last_score"])
    except Exception:
        pass
    return None


def _predictor_confidence(name: str, symbol: str, narrative: str) -> tuple[int, str]:
    """
    Run the launch predictor with a minimal feature vector built from
    name/symbol/narrative.  Returns (confidence 0-100, archetype_key).
    Falls back to (0, "NONE") on any error.
    """
    try:
        import launch_predictor as _lp
        # Build minimal token dict — only name/symbol/mint known at snipe time
        token = {"mint": "", "name": name, "symbol": symbol, "mcap": 0, "dex": "pumpfun",
                 "pair_created": time.time() * 1000}
        # Inject narrative into breakdown so the predictor can do archetype matching
        reason_str = f"narrative: {narrative}" if narrative != "Other" else ""
        breakdown  = {"narrative": (15 if narrative != "Other" else 0, reason_str)}
        result = _lp.predict_launch(token, {}, breakdown)
        return result.get("confidence", 0), result.get("archetype", "NONE")
    except Exception as e:
        logger.debug(f"[Sniper] predictor error: {e}")
        return 0, "NONE"


def _bundle_risk_cached(creator_wallet: str) -> int:
    """
    Check bundle_log for cached risk level for this creator wallet.
    Returns 0 if not seen before (no penalty for unknown wallets).
    Uses only the in-memory/file cache — no RPC calls.
    """
    if not creator_wallet:
        return 0
    try:
        import wallet_fingerprint as _wf
        funder = _wf.get_cached_funder(creator_wallet)
        if funder is None:
            return 0
        # If this wallet was previously identified as a funder of bundle clusters,
        # treat it as medium-risk
        bundle_log = _wf.get_bundle_log(limit=50)
        for entry in bundle_log:
            for cluster in entry.get("clusters", []):
                if cluster.get("funder") == creator_wallet:
                    return entry.get("bundle_risk", 5)
    except Exception:
        pass
    return 0


def _resolve_live_buy_fill(actual_tokens: int, trade_sol: float) -> tuple[bool, float, float]:
    """
    Decide whether a confirmed live buy produced a real position.

    Returns:
        (opened, tokens_bought, buy_price_sol)
    """
    if actual_tokens <= 0:
        return False, 0.0, 0.0
    buy_price_sol = (trade_sol / actual_tokens) if actual_tokens else 0.0
    return True, float(actual_tokens), buy_price_sol


def _record_buy_attempt(
    uid: int,
    mint: str,
    symbol: str,
    name: str,
    trade_sol: float,
    tx_sig: str,
    tx_status: str,
    tokens_received: float,
    sol_before: float,
    sol_after: float,
    outcome: str,
    note: str = "",
    *,
    mode: str = "live",
    attempted_at: Optional[float] = None,
):
    attempted_at = attempted_at or time.time()
    sol_delta = sol_before - sol_after
    _db._exec("""
        INSERT INTO sniper_buy_attempts
            (uid, mint, symbol, name, mode, trade_sol, tx_sig, tx_status,
             tokens_received, sol_before, sol_after, sol_delta, outcome, note, attempted_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        uid, mint, symbol, name, mode, trade_sol, tx_sig, tx_status,
        tokens_received, sol_before, sol_after, sol_delta, outcome, note, attempted_at,
    ))


def _recent_dangerous_buy_count(uid: int, *, now_ts: Optional[float] = None) -> int:
    now_ts = now_ts or time.time()
    since_ts = now_ts - ZERO_TOKEN_CB_WINDOW_SECS
    row = _db._fetchone("""
        SELECT COUNT(*) AS n
        FROM sniper_buy_attempts
        WHERE uid = ?
          AND attempted_at >= ?
          AND tokens_received <= 0
          AND sol_delta > 0.0005
    """, (uid, since_ts))
    return row["n"] if row else 0


def _trip_zero_token_circuit_breaker(cfg: SniperConfig) -> int:
    """
    Disable sniper after repeated zero-token live buys in a short window.
    Returns the current zero-token count in the breaker window.
    """
    zero_count = _recent_dangerous_buy_count(cfg.uid)
    if zero_count >= ZERO_TOKEN_CB_THRESHOLD and cfg.enabled:
        cfg.enabled = False
        _save_config(cfg)
    return zero_count


# ── Engine ────────────────────────────────────────────────────────────────────

class SniperEngine:
    """
    Per-uid pump.fun sniper.

    One global instance per uid is maintained by the module-level registry
    (see get_engine). Each engine runs a single asyncio background task that
    polls open positions and exits them on TP/SL/time rules.
    """

    def __init__(self, uid: int):
        self.uid = uid
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # in-memory set of mints currently being sniped (to avoid races)
        self._pending: set[str] = set()
        # in-memory set of mints currently being exited (avoid duplicate sells)
        self._exiting: set[str] = set()
        # In-memory kill switch so concurrent fanout stops quickly once breaker trips.
        self._breaker_tripped = False
        # Track consecutive zero-balance readings per mint to avoid closing
        # positions on transient RPC errors (require 2 consecutive zeros).
        self._zero_balance_strikes: dict[str, int] = {}
        # Track total sell failures per mint across poll cycles.
        # After MAX_SELL_CYCLES_BEFORE_ABANDON failures, force-close the position.
        self._sell_fail_counts: dict[str, int] = {}

    def _active_slots_locked(self) -> tuple[int, int, int]:
        """Return (open_positions, pending_positions, total_active). Call under self._lock."""
        open_positions = _open_position_count(self.uid)
        pending_positions = len(self._pending)
        return open_positions, pending_positions, open_positions + pending_positions

    def _buy_path_halted(self) -> bool:
        """Hard stop for in-flight live buys once the circuit breaker trips."""
        return self._breaker_tripped or not _get_config(self.uid).enabled

    # ── Snipe entry point ─────────────────────────────────────────────────────

    async def attempt_snipe(
        self,
        mint: str,
        symbol: str = "",
        name: str = "",
        creator_wallet: str = "",
        pumpfeed_event: dict | None = None,
    ) -> str:
        """
        Try to snipe a newly launched token.
        Returns a short status string (for logging).
        Called from helius_ws._handle_launch() or pumpfeed create events.
        """
        cfg = _get_config(self.uid)
        if cfg.enabled:
            self._breaker_tripped = False
        if not cfg.enabled:
            return "disabled"
        if self._breaker_tripped:
            return "circuit_breaker"

        # ── Time-of-day gate ──────────────────────────────────────────────────
        if not _check_active_hours(cfg.active_hours_utc):
            return "outside_hours"

        if mint in self._pending:
            return "pending"

        async with self._lock:
            if _already_sniped(self.uid, mint):
                return "already_bought"
            open_positions, pending_positions, total_active = self._active_slots_locked()
            if total_active >= cfg.max_concurrent:
                logger.info(
                    f"[Sniper] uid={self.uid} {symbol or mint[:8]} blocked by max_concurrent "
                    f"(open={open_positions} pending={pending_positions} cap={cfg.max_concurrent})"
                )
                return f"max_concurrent ({cfg.max_concurrent})"
            if self._breaker_tripped:
                return "circuit_breaker"
            self._pending.add(mint)
        try:
            result = await self._execute_snipe(mint, symbol, name, creator_wallet, cfg, pumpfeed_event)
            if result.startswith("bought"):
                logger.info(f"[Sniper] uid={self.uid} {symbol or mint[:8]} → {result}")
            return result
        finally:
            self._pending.discard(mint)

    async def _execute_snipe(
        self,
        mint: str,
        symbol: str,
        name: str,
        creator_wallet: str,
        cfg: SniperConfig,
        pumpfeed_event: dict | None = None,
    ) -> str:
        rpc  = getattr(_cfg, "SOLANA_RPC", "https://api.mainnet-beta.solana.com")
        loop = asyncio.get_running_loop()

        # ── Build bonding curve data ───────────────────────────────────────────
        # If we have pumpfeed event data, build a synthetic BC dict immediately
        # (avoids waiting for RPC to index the account). For live buys we still
        # fetch fresh on-chain data before executing.
        bc = None
        if pumpfeed_event:
            tokens_in_pool = pumpfeed_event.get("tokensInPool", 0)
            sol_in_pool    = pumpfeed_event.get("solInPool", 0)
            initial_buy    = pumpfeed_event.get("initialBuy", 0)
            pool_type      = pumpfeed_event.get("pool", "")
            if not tokens_in_pool and not sol_in_pool:
                # No initial buy — use pump.fun default reserves (empty curve)
                tokens_in_pool = 1_000_000_000  # 1B tokens
                sol_in_pool = 0.0
            if tokens_in_pool:
                TOTAL_SUPPLY = 1_000_000_000_000_000  # pump.fun standard 1B * 1e6
                real_token_reserves = int(tokens_in_pool * 1_000_000)
                real_sol_reserves   = int(sol_in_pool * 1_000_000_000)
                # pump.fun virtual offsets: 1073B tokens, 30 SOL
                virt_token = real_token_reserves + 279_900_000_000_000
                virt_sol   = real_sol_reserves + 30_000_000_000
                bc = {
                    "bonding_curve":          "pumpfeed",
                    "virtual_token_reserves": virt_token,
                    "virtual_sol_reserves":   virt_sol,
                    "real_token_reserves":    real_token_reserves,
                    "real_sol_reserves":      real_sol_reserves,
                    "token_total_supply":     TOTAL_SUPPLY,
                    "complete":               False,
                }

        if not bc:
            # Fallback: fetch from RPC (helius_ws path or pumpfeed data missing)
            await asyncio.sleep(7)
            bc = await loop.run_in_executor(None, fetch_bonding_curve_data, mint, rpc)
            if not bc:
                await asyncio.sleep(5)
                bc = await loop.run_in_executor(None, fetch_bonding_curve_data, mint, rpc)
        if not bc:
            return "no_bonding_curve"
        if bc.get("complete"):
            return "graduated"

        # ── Filter: dev buy check ──────────────────────────────────────────────
        total_supply = bc.get("token_total_supply", 0)
        real_tokens  = bc.get("real_token_reserves", 0)
        if total_supply and real_tokens:
            dev_bought_pct = (total_supply - real_tokens) / total_supply * 100
            if dev_bought_pct > cfg.dev_buy_max_pct:
                logger.info(
                    f"[Sniper] {mint[:8]} skipped — dev bought {dev_bought_pct:.1f}%"
                )
                return f"dev_buy_{dev_bought_pct:.0f}pct"

        # ── Filter: lifecycle cross-reference ─────────────────────────────────
        # If the scanner has already seen and scored this token, use that signal.
        lifecycle_score: Optional[float] = None
        if cfg.use_lifecycle_filter:
            lifecycle_score = _lifecycle_score(mint)
            if lifecycle_score is not None and lifecycle_score < 50:
                logger.info(
                    f"[Sniper] {mint[:8]} skipped — lifecycle score {lifecycle_score:.0f} < 50"
                )
                return f"lifecycle_low_{lifecycle_score:.0f}"

        # ── Filter: narrative pre-filter ──────────────────────────────────────
        narrative_matched, narrative_name = _detect_narrative(name, symbol)
        if cfg.require_narrative and not narrative_matched:
            logger.info(f"[Sniper] {mint[:8]} skipped — no narrative match ({name} {symbol})")
            return "no_narrative"

        # ── Filter: bundle risk (cache only, no RPC) ──────────────────────────
        if cfg.max_bundle_risk < 10 and creator_wallet:
            bundle_risk = await loop.run_in_executor(
                None, _bundle_risk_cached, creator_wallet
            )
            if bundle_risk > cfg.max_bundle_risk:
                logger.info(
                    f"[Sniper] {mint[:8]} skipped — bundle risk {bundle_risk} > {cfg.max_bundle_risk}"
                )
                return f"bundle_risk_{bundle_risk}"

        # ── Intelligence: launch predictor ────────────────────────────────────
        predictor_confidence, predictor_archetype = 0, "NONE"
        if cfg.min_predictor_confidence > 0 or cfg.sol_multiplier_predictor != 1.0:
            predictor_confidence, predictor_archetype = await loop.run_in_executor(
                None, _predictor_confidence, name, symbol, narrative_name
            )
            if predictor_confidence < cfg.min_predictor_confidence:
                logger.info(
                    f"[Sniper] {mint[:8]} skipped — predictor confidence "
                    f"{predictor_confidence} < {cfg.min_predictor_confidence:.0f}"
                )
                return f"low_confidence_{predictor_confidence}"

        # ── Adaptive position sizing ───────────────────────────────────────────
        trade_sol = cfg.sol_amount
        # Lifecycle boost: double-down when scanner already rated it hot
        if lifecycle_score is not None and lifecycle_score >= 70:
            trade_sol = min(trade_sol * 2.0, cfg.sol_amount * 3.0)
            logger.info(f"[Sniper] {mint[:8]} lifecycle boost — 2x size (score {lifecycle_score:.0f})")
        # Narrative multiplier
        if narrative_matched:
            trade_sol *= cfg.sol_multiplier_narrative
        # Predictor multiplier (only when confidence is genuinely high)
        if predictor_confidence >= 70:
            trade_sol *= cfg.sol_multiplier_predictor

        # ── Determine trade mode ────────────────────────────────────────────────
        mode = _get_user_mode(self.uid)

        # ── Execute buy ────────────────────────────────────────────────────────
        sol_lamports  = int(trade_sol * 1_000_000_000)
        tokens_bought = calculate_buy_tokens(sol_lamports, bc)
        buy_price_sol = (trade_sol / tokens_bought) if tokens_bought else 0

        if mode == "live":
            try:
                kp = _get_keypair()
            except RuntimeError as e:
                logger.error(f"[Sniper] keypair error: {e}")
                return "no_keypair"

            wallet_str = str(kp.pubkey())

            # Pre-check SOL balance to avoid wasting gas on doomed txs
            sol_balance = await loop.run_in_executor(
                None, get_sol_balance, wallet_str, rpc
            )
            # Need trade_sol + ~0.01 SOL buffer for priority fees, rent, tx fees
            min_required = trade_sol + 0.01
            if sol_balance < min_required:
                logger.warning(
                    f"[Sniper] {mint[:8]} skipped — insufficient SOL: "
                    f"{sol_balance:.4f} < {min_required:.4f} needed"
                )
                return f"low_balance:{sol_balance:.4f}"
            if self._buy_path_halted():
                return "circuit_breaker"

            sig = await _retry_live_buy_on_curve_race(
                mint, trade_sol, kp, rpc, loop
            )

            if sig.startswith("ERROR") or sig == "GRADUATED":
                logger.warning(f"[Sniper] buy failed {mint[:8]}: {sig}")
                _record_buy_attempt(
                    self.uid,
                    mint,
                    symbol or mint[:6],
                    name or mint[:8],
                    trade_sol,
                    sig if not sig.startswith("ERROR") else "",
                    "preflight_error" if sig.startswith("ERROR") else sig,
                    0.0,
                    sol_balance,
                    sol_balance,
                    "buy_failed",
                    sig,
                )
                return f"buy_failed:{sig}"

            tx_status = await _poll_tx_confirmation(sig, rpc)
            if tx_status != "confirmed":
                sol_after = await loop.run_in_executor(
                    None, get_sol_balance, wallet_str, rpc
                )
                _record_buy_attempt(
                    self.uid,
                    mint,
                    symbol or mint[:6],
                    name or mint[:8],
                    trade_sol,
                    sig,
                    tx_status,
                    0.0,
                    sol_balance,
                    sol_after,
                    "buy_unconfirmed",
                    f"tx_status={tx_status}",
                )
                logger.warning(
                    f"[Sniper] buy unconfirmed {mint[:8]}: tx={sig} status={tx_status}"
                )
                if cfg.telegram_notify:
                    message = (
                        f"⚠️ *Sniper Buy Not Confirmed* `[{mode.upper()}]` — `{symbol or mint[:6]}`\n"
                        f"Tx: `{sig}`\n"
                        f"Status: `{tx_status}`\n"
                        f"Position was not opened."
                    )
                    asyncio.create_task(_send_notify(self.uid, message))
                return f"buy_unconfirmed:{tx_status}"

            # Read actual on-chain balance (retry-aware — avoids false 0 from RPC errors)
            actual_tokens = await _get_token_balance_reliable(
                str(kp.pubkey()), mint, rpc, loop
            )

            # RPC completely unreachable — can't verify buy landed.
            # Do NOT open position with an unverified estimate; the sell loop
            # wouldn't be able to sell reliably either if RPC is down.
            # CRITICAL: trip the circuit breaker so no more buys leak through
            # while RPC is down (each unverified buy doesn't count toward
            # max_concurrent, which would let unlimited buys through).
            if actual_tokens is None:
                self._breaker_tripped = True
                logger.error(
                    f"[Sniper] {mint[:8]} balance check failed (RPC errors) after confirmed tx — "
                    f"cannot verify buy; NOT opening position. CIRCUIT BREAKER TRIPPED."
                )
                _record_buy_attempt(
                    self.uid, mint, symbol or mint[:6], name or mint[:8],
                    trade_sol, sig, tx_status, 0.0,
                    sol_balance, sol_balance, "rpc_balance_error",
                    "confirmed tx but all balance checks failed — position not opened",
                )
                if cfg.telegram_notify:
                    asyncio.create_task(_send_notify(
                        self.uid,
                        f"🛑 *Sniper* — `{symbol or mint[:6]}` buy tx confirmed but "
                        f"balance check failed (RPC errors).\n"
                        f"Position NOT opened. Check wallet manually.\n"
                        f"Tx: `{sig}`\n"
                        f"⚠️ Circuit breaker tripped — sniper paused until re-enabled.",
                    ))
                return "buy_rpc_error"

            opened, resolved_tokens, resolved_buy_price_sol = _resolve_live_buy_fill(
                actual_tokens,
                trade_sol,
            )
            sol_after = await loop.run_in_executor(
                None, get_sol_balance, wallet_str, rpc
            )
            if opened:
                tokens_bought = resolved_tokens
                buy_price_sol = resolved_buy_price_sol
                _record_buy_attempt(
                    self.uid,
                    mint,
                    symbol or mint[:6],
                    name or mint[:8],
                    trade_sol,
                    sig,
                    tx_status,
                    resolved_tokens,
                    sol_balance,
                    sol_after,
                    "opened",
                )
            else:
                _record_buy_attempt(
                    self.uid,
                    mint,
                    symbol or mint[:6],
                    name or mint[:8],
                    trade_sol,
                    sig,
                    tx_status,
                    0.0,
                    sol_balance,
                    sol_after,
                    "zero_tokens",
                    "confirmed but wallet ATA balance was 0 after retries",
                )
                zero_count = _trip_zero_token_circuit_breaker(cfg)
                if zero_count >= ZERO_TOKEN_CB_THRESHOLD:
                    self._breaker_tripped = True
                logger.warning(
                    f"[Sniper] buy confirmed but ATA balance is 0 {mint[:8]} — refusing to open position"
                )
                if zero_count >= ZERO_TOKEN_CB_THRESHOLD:
                    logger.error(
                        f"[Sniper] uid={self.uid} circuit breaker tripped after {zero_count} zero-token buys in "
                        f"{ZERO_TOKEN_CB_WINDOW_SECS}s — sniper disabled"
                    )
                if cfg.telegram_notify:
                    lines = [
                        f"⚠️ *Sniper Buy Rejected* `[{mode.upper()}]` — `{symbol or mint[:6]}`\n"
                        f"Tx: `{sig}`\n"
                        f"Status: `confirmed but 0 tokens received`\n"
                        f"Position was not opened."
                    ]
                    if zero_count >= ZERO_TOKEN_CB_THRESHOLD:
                        lines.append(
                            f"🛑 Circuit breaker triggered after `{zero_count}` zero-token buys. Sniper disabled."
                        )
                    asyncio.create_task(_send_notify(self.uid, "\n".join(lines)))
                return "buy_no_tokens"
        else:
            # Paper mode — simulate buy using bonding curve math, no on-chain tx
            sig = f"PAPER_{int(time.time())}_{mint[:8]}"
            if not tokens_bought:
                return "paper_no_tokens"

        # ── Record position ────────────────────────────────────────────────────

        overcap = False
        async with self._lock:
            # Second guard: state may have changed while this buy was in flight.
            open_positions = _open_position_count(self.uid)
            other_pending = max(0, len(self._pending) - 1)
            active_without_current = open_positions + other_pending
            if active_without_current >= cfg.max_concurrent:
                if mode == "live":
                    # NEVER orphan tokens — record position even if over cap,
                    # and flag for immediate sell so we recover the SOL.
                    logger.warning(
                        f"[Sniper] uid={self.uid} {symbol or mint[:8]} over max_concurrent "
                        f"(open={open_positions} other_pending={other_pending} cap={cfg.max_concurrent}) "
                        f"— recording position anyway to prevent orphaned tokens"
                    )
                    overcap = True
                else:
                    logger.info(
                        f"[Sniper] uid={self.uid} {symbol or mint[:8]} dropped before record by max_concurrent "
                        f"(open={open_positions} other_pending={other_pending} cap={cfg.max_concurrent})"
                    )
                    return f"max_concurrent ({cfg.max_concurrent})"

            pos = SniperPosition(
                uid=self.uid,
                mint=mint,
                symbol=symbol or mint[:6],
                name=name or mint[:8],
                tokens_bought=float(tokens_bought),
                sol_spent=trade_sol,
                buy_price_sol=buy_price_sol,
                buy_time=time.time(),
                mode=mode,
            )
            _insert_position(pos)

        # Build context tags for log/notify
        tags = []
        if narrative_matched:
            tags.append(f"#{narrative_name}")
        if predictor_archetype != "NONE":
            tags.append(f"arch:{predictor_archetype}")
        if lifecycle_score is not None and lifecycle_score >= 70:
            tags.append(f"score:{lifecycle_score:.0f}")
        tag_str = " ".join(tags)

        mode_tag = "PAPER" if mode == "paper" else "LIVE"
        logger.info(
            f"[Sniper] [{mode_tag}] BOUGHT {pos.symbol} ({mint[:8]}) "
            f"— {trade_sol:.4f} SOL / {tokens_bought:.0f} tokens "
            f"— tx {sig} {tag_str}"
        )

        # ── Telegram notification ──────────────────────────────────────────────
        if cfg.telegram_notify:
            mode_emoji = "📝" if mode == "paper" else "💰"
            notify_lines = [
                f"🎯 *Sniper Buy* {mode_emoji} `[{mode.upper()}]` — `{pos.symbol}`",
                f"Mint: `{mint[:16]}…`",
                f"Spent: `{trade_sol:.4f} SOL`",
                f"Tx: `{sig[:20]}…`",
            ]
            if tag_str:
                notify_lines.append(f"Signals: {tag_str}")
            asyncio.create_task(_send_notify(self.uid, "\n".join(notify_lines)))

        # Ensure auto-sell loop is running
        self._ensure_sell_loop()

        # If over cap, immediately sell to recover SOL (don't let tokens sit idle)
        if overcap:
            logger.info(
                f"[Sniper] {symbol or mint[:8]} over cap — scheduling immediate sell"
            )
            rpc = getattr(_cfg, "SOLANA_RPC", "https://api.mainnet-beta.solana.com")
            asyncio.create_task(self._exit_position(pos, "OVERCAP", loop, rpc))

        return f"bought:{sig[:16]}"

    # ── Auto-sell loop ─────────────────────────────────────────────────────────

    def _ensure_sell_loop(self):
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._sell_loop())

    def _resume_open_positions_if_needed(self):
        """Restart the sell loop for persisted open positions after process restart."""
        if self._task and not self._task.done():
            return
        if _open_position_count(self.uid) <= 0:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._ensure_sell_loop()

    async def _sell_loop(self):
        """Poll open positions and exit on TP / SL / time rules."""
        rpc  = getattr(_cfg, "SOLANA_RPC", "https://api.mainnet-beta.solana.com")
        loop = asyncio.get_running_loop()

        while self._running:
            try:
                await asyncio.sleep(POLL_INTERVAL_SECS)

                positions = _load_open_positions(self.uid)
                if not positions:
                    self._running = False
                    return

                cfg = _get_config(self.uid)

                for pos in positions:
                    reason, bc = await self._check_exit(pos, cfg, loop, rpc)
                    if reason:
                        result = await self._exit_position(pos, reason, loop, rpc, bc=bc)
                        if result.get("ok"):
                            # Sell succeeded — clear failure counter
                            self._sell_fail_counts.pop(pos.mint, None)
                        elif (result.get("error", "").startswith("sell_failed")
                              or result.get("error") == "sell_unconfirmed"):
                            # Track sell failures; force-close after too many cycles
                            fails = self._sell_fail_counts.get(pos.mint, 0) + 1
                            self._sell_fail_counts[pos.mint] = fails
                            if fails >= _MAX_SELL_CYCLES:
                                logger.error(
                                    f"[Sniper] {pos.symbol} ({pos.mint[:8]}) sell failed "
                                    f"{fails} cycles — force-closing as SELL_ABANDONED"
                                )
                                _close_position_db(self.uid, pos.mint, 0.0, "SELL_ABANDONED")
                                self._sell_fail_counts.pop(pos.mint, None)
                                if cfg.telegram_notify:
                                    asyncio.create_task(_send_notify(
                                        self.uid,
                                        f"🛑 *Sniper* — `{pos.symbol}` sell failed {fails}x.\n"
                                        f"Position force-closed. Tokens may still be in wallet.",
                                    ))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[Sniper] _sell_loop iteration error — retrying")

    async def _check_exit(
        self,
        pos: SniperPosition,
        cfg: SniperConfig,
        loop: asyncio.AbstractEventLoop,
        rpc: str,
    ) -> tuple[Optional[str], Optional[dict]]:
        """Return (exit_reason, bonding_curve_data) if position should close, else (None, None)."""
        age = time.time() - pos.buy_time

        # Time stop
        if age >= cfg.max_age_secs:
            return "TIME", None

        # Price check
        bc = await loop.run_in_executor(None, fetch_bonding_curve_data, pos.mint, rpc)
        if not bc:
            # For very fresh positions (<60s), RPC may not have indexed yet — skip this cycle
            if age < 60:
                return None, None
            return "NO_CURVE", None
        if bc.get("complete"):
            return "GRADUATED", bc

        vtr = bc.get("virtual_token_reserves", 0)
        vsr = bc.get("virtual_sol_reserves", 0)
        if not vtr or not pos.buy_price_sol:
            return None, None

        current_price_sol = vsr / vtr / 1e9
        pnl_pct = (current_price_sol - pos.buy_price_sol) / pos.buy_price_sol * 100

        if pnl_pct >= cfg.take_profit_pct:
            return "TP", bc
        if pnl_pct <= -cfg.stop_loss_pct:
            return "SL", bc

        return None, None

    async def _exit_position(
        self,
        pos: SniperPosition,
        reason: str,
        loop: asyncio.AbstractEventLoop,
        rpc: str,
        *,
        bc: Optional[dict] = None,
    ) -> dict:
        async with self._lock:
            if pos.mint in self._exiting:
                return {"ok": False, "error": "exit_in_progress"}
            self._exiting.add(pos.mint)

        try:
            cfg = _get_config(self.uid)

            if reason == "GRADUATED":
                # Can't sell graduated tokens on bonding curve — close as stranded
                _close_position_db(self.uid, pos.mint, 0.0, "GRADUATED_STRANDED")
                mode = getattr(pos, "mode", "live")
                mode_tag = "PAPER" if mode == "paper" else "LIVE"
                logger.warning(
                    f"[Sniper] [{mode_tag}] {pos.symbol} graduated before sell — position stranded"
                )
                if cfg.telegram_notify:
                    asyncio.create_task(_send_notify(
                        self.uid,
                        f"⚠️ *Sniper* \\[{mode_tag}\\] — `{pos.symbol}` graduated to Raydium before sell.\n"
                        f"Position stranded — manual intervention needed.",
                    ))
                return {"ok": True, "exit_reason": "GRADUATED_STRANDED"}

            mode = getattr(pos, "mode", "live")
            mode_tag = "PAPER" if mode == "paper" else "LIVE"

            if mode == "live":
                try:
                    kp = _get_keypair()
                except RuntimeError:
                    return {"ok": False, "error": "no_keypair"}

                # Read actual on-chain token balance (retry-aware to avoid RPC false-zeros)
                actual_balance = await _get_token_balance_reliable(
                    str(kp.pubkey()), pos.mint, rpc, loop
                )

                # RPC completely unreachable — skip this sell cycle, don't kill position
                if actual_balance is None:
                    logger.warning(
                        f"[Sniper] {pos.mint[:8]} balance check failed (RPC errors) — "
                        f"skipping sell cycle, will retry next poll"
                    )
                    return {"ok": False, "error": "balance_check_rpc_error"}

                if actual_balance <= 0:
                    # Require 2 consecutive zero readings before closing position
                    strikes = self._zero_balance_strikes.get(pos.mint, 0) + 1
                    self._zero_balance_strikes[pos.mint] = strikes
                    if strikes < 2:
                        logger.warning(
                            f"[Sniper] {pos.mint[:8]} ATA has 0 tokens (strike {strikes}/2) — "
                            f"will recheck next poll before closing"
                        )
                        return {"ok": False, "error": "zero_balance_strike_1"}
                    # Confirmed zero after 2 consecutive checks
                    self._zero_balance_strikes.pop(pos.mint, None)
                    logger.warning(
                        f"[Sniper] {pos.mint[:8]} ATA has 0 tokens (2 consecutive checks) — closing position"
                    )
                    _close_position_db(self.uid, pos.mint, 0.0, "NO_TOKENS")
                    if cfg.telegram_notify:
                        asyncio.create_task(_send_notify(
                            self.uid,
                            f"⚠️ *Sniper* \\[{mode_tag}\\] — `{pos.symbol}` has 0 tokens in wallet.\n"
                            f"Buy may have failed on-chain. Position closed with 0 SOL.",
                        ))
                    return {"ok": True, "exit_reason": "NO_TOKENS"}
                # Balance is positive — clear any prior zero strikes
                self._zero_balance_strikes.pop(pos.mint, None)
                token_amount = actual_balance
            else:
                token_amount = int(pos.tokens_bought)

            # Estimate SOL received from bonding curve (used for both paper & live P&L)
            sol_received = 0.0
            if bc is None:
                bc = await loop.run_in_executor(
                    None, fetch_bonding_curve_data, pos.mint, rpc
                )
            if bc:
                vtr = bc.get("virtual_token_reserves", 1)
                vsr = bc.get("virtual_sol_reserves", 0)
                sol_received = (vsr - int(vtr * vsr / (vtr + token_amount))) * 0.99 / 1e9

            if mode == "live":
                # Snapshot SOL balance before sell to measure actual proceeds
                wallet_str = str(kp.pubkey())
                sol_before = await loop.run_in_executor(
                    None, get_sol_balance, wallet_str, rpc
                )

                # Retry sell up to 3 times with fresh blockhash each attempt
                sig = ""
                for sell_attempt in range(1, 4):
                    sig = await loop.run_in_executor(
                        None, sell_pumpfun, pos.mint, token_amount, kp, rpc
                    )

                    if sig.startswith("ERROR") or sig == "GRADUATED":
                        logger.warning(f"[Sniper] sell failed {pos.mint[:8]}: {sig}")
                        if sig == "GRADUATED":
                            _close_position_db(self.uid, pos.mint, 0.0, "GRADUATED_STRANDED")
                            return {"ok": True, "exit_reason": "GRADUATED_STRANDED"}
                        return {"ok": False, "error": f"sell_failed:{sig}"}

                    # Confirm the sell tx actually landed on-chain
                    tx_status = await _poll_tx_confirmation(sig, rpc, max_polls=20, poll_interval=1.5)
                    if tx_status == "confirmed":
                        break
                    logger.warning(
                        f"[Sniper] sell tx {tx_status} {pos.mint[:8]} attempt {sell_attempt}/3 tx={sig[:16]}"
                    )
                    if sell_attempt < 3:
                        await asyncio.sleep(1)
                else:
                    logger.warning(f"[Sniper] sell gave up after 3 attempts {pos.mint[:8]}")
                    return {"ok": False, "error": "sell_unconfirmed"}

                # Measure actual SOL received from sell (balance delta)
                sol_after = await loop.run_in_executor(
                    None, get_sol_balance, wallet_str, rpc
                )
                actual_sol = sol_after - sol_before
                if actual_sol > 0:
                    sol_received = actual_sol
            else:
                sig = f"PAPER_SELL_{int(time.time())}_{pos.mint[:8]}"

            _close_position_db(self.uid, pos.mint, sol_received, reason)
            profit     = sol_received - pos.sol_spent
            profit_pct = (profit / pos.sol_spent * 100) if pos.sol_spent else 0

            logger.info(
                f"[Sniper] [{mode_tag}] SOLD {pos.symbol} ({pos.mint[:8]}) "
                f"reason={reason} profit={profit:+.4f} SOL ({profit_pct:+.1f}%) tx={sig[:16] if sig else 'n/a'}"
            )

            # ── Telegram notification ──────────────────────────────────────────────
            if cfg.telegram_notify:
                exit_emoji = {"TP": "✅", "SL": "🛑", "TIME": "⏱️", "MANUAL": "🤙",
                              "NO_CURVE": "👻", "OVERCAP": "🔄",
                              "SELL_ABANDONED": "🛑"}.get(reason, "📤")
                mode_emoji = "📝" if mode == "paper" else "💰"
                profit_str = f"{profit:+.5f} SOL ({profit_pct:+.1f}%)"
                asyncio.create_task(_send_notify(
                    self.uid,
                    f"{exit_emoji} *Sniper Sell* {mode_emoji} `[{mode_tag}]` — `{pos.symbol}`\n"
                    f"Exit: `{reason}` | P&L: `{profit_str}`\n"
                    f"Held: `{int(time.time() - pos.buy_time)}s`",
                ))
            return {"ok": True, "exit_reason": reason}
        finally:
            async with self._lock:
                self._exiting.discard(pos.mint)

    # ── Manual close ──────────────────────────────────────────────────────────

    async def close_position(self, mint: str) -> dict:
        """Manually close an open position. Returns result dict."""
        positions = _load_open_positions(self.uid)
        pos = next((p for p in positions if p.mint == mint), None)
        if not pos:
            return {"ok": False, "error": "position not found"}

        rpc  = getattr(_cfg, "SOLANA_RPC", "https://api.mainnet-beta.solana.com")
        loop = asyncio.get_running_loop()
        result = await self._exit_position(pos, "MANUAL", loop, rpc)
        if result.get("ok"):
            return {"ok": True, "mint": mint, "exit_reason": result.get("exit_reason", "MANUAL")}
        return {"ok": False, "error": result.get("error", "close_failed")}

    # ── Read helpers ──────────────────────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        """Return open positions enriched with current P&L."""
        rpc = getattr(_cfg, "SOLANA_RPC", "https://api.mainnet-beta.solana.com")
        positions = _load_open_positions(self.uid)
        result = []
        for pos in positions:
            bc = fetch_bonding_curve_data(pos.mint, rpc)
            current_price_sol = 0.0
            unrealized_sol    = 0.0
            pnl_pct           = 0.0
            if bc and not bc.get("complete"):
                vtr = bc.get("virtual_token_reserves", 0)
                vsr = bc.get("virtual_sol_reserves", 0)
                if vtr:
                    current_price_sol = vsr / vtr / 1e9
                    current_value     = current_price_sol * pos.tokens_bought
                    unrealized_sol    = current_value - pos.sol_spent
                    if pos.sol_spent:
                        pnl_pct = unrealized_sol / pos.sol_spent * 100

            result.append({
                "mint":              pos.mint,
                "symbol":            pos.symbol,
                "name":              pos.name,
                "sol_spent":         pos.sol_spent,
                "tokens_bought":     pos.tokens_bought,
                "buy_price_sol":     pos.buy_price_sol,
                "current_price_sol": current_price_sol,
                "unrealized_sol":    round(unrealized_sol, 6),
                "pnl_pct":           round(pnl_pct, 2),
                "buy_time":          pos.buy_time,
                "age_secs":          round(time.time() - pos.buy_time),
                "mode":              getattr(pos, "mode", "live"),
            })
        return result

    def get_history(self, limit: int = 50) -> list[dict]:
        rows = _db._fetchall(
            "SELECT * FROM sniper_history WHERE uid = ? ORDER BY sell_time DESC LIMIT ?",
            (self.uid, limit),
        )
        return [dict(r) for r in rows]

    def get_buy_attempts(self, limit: int = 50) -> list[dict]:
        rows = _db._fetchall(
            "SELECT * FROM sniper_buy_attempts WHERE uid = ? ORDER BY attempted_at DESC LIMIT ?",
            (self.uid, limit),
        )
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        day_start = _db._utc_day_start_ts()
        rows = _db._fetchall(
            "SELECT profit_sol, sol_spent FROM sniper_history WHERE uid = ? AND sell_time >= ?",
            (self.uid, day_start),
        )
        total      = len(rows)
        wins       = sum(1 for r in rows if r["profit_sol"] > 0)
        profit_sol = sum(r["profit_sol"] for r in rows)
        open_count = _open_position_count(self.uid)
        return {
            "snipes_today":      total,
            "win_rate_pct":      round(wins / total * 100, 1) if total else 0,
            "profit_sol_today":  round(profit_sol, 5),
            "open_positions":    open_count,
        }

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None


# ── Engine registry ────────────────────────────────────────────────────────────
# One SniperEngine instance per uid, created on demand.

_engines: dict[int, SniperEngine] = {}


def get_engine(uid: int) -> SniperEngine:
    """Return (or create) the SniperEngine for a given uid."""
    if uid not in _engines:
        _engines[uid] = SniperEngine(uid)
    engine = _engines[uid]
    engine._resume_open_positions_if_needed()
    return engine


def all_enabled_uids() -> list[int]:
    """Return uids with sniper enabled — used by helius_ws to fan out snipes."""
    rows = _db._fetchall(
        "SELECT uid FROM sniper_config WHERE enabled = 1", ()
    )
    return [r["uid"] for r in rows]
