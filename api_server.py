"""
DigitalDegenX Bot — AI Control API
====================================
A FastAPI server that runs alongside bot.py and exposes authenticated REST
endpoints any AI model/agent can call to read state and drive trades.

Start standalone:
    uvicorn api_server:app --host 0.0.0.0 --port 8080

Or launched automatically from bot.py (see integration below).

Authentication:
    Pass your API key in every request header:
        X-API-Key: <your key from config.py API_KEY>

Available endpoints:
    GET  /status              — bot health + mode
    GET  /portfolio           — paper portfolio balances
    GET  /wallet              — live wallet SOL + token balances
    GET  /scanner/top         — top scanner alerts from today
    GET  /scanner/watchlist   — current watchlist (scored tokens)
    GET  /price/{mint}        — token price + mcap from DexScreener
    GET  /token/{mint}/safety — RugCheck safety summary
    POST /buy                 — paper or live buy
    POST /sell                — paper or live sell
    POST /alert/set           — set a price alert
    POST /scanner/threshold   — update watch/alert score thresholds
    GET  /trades              — recent trade history
    GET  /autosell/{mint}     — auto-sell config for a token
    POST /autosell/{mint}     — update auto-sell config
    POST /message             — send a Telegram message to a user
"""

import json
import os
import time
import asyncio
import secrets
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Optional, Union

from fastapi import FastAPI, HTTPException, Security, Depends, Query
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import config as _cfg
import db as _db
import settings_manager as sm
import trade_center as tc
import research_logger
from services.lifecycle import store as lifecycle_store
from services.trading import build_trading_snapshot

# ─── Config ───────────────────────────────────────────────────────────────────

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
CONFIG_DIR = os.path.dirname(__file__)
LIVE_WALLET_META_FILE = os.path.join(DATA_DIR, "live_wallet_meta.json")

def _load_api_key() -> str:
    try:
        import config as _cfg
        key = getattr(_cfg, "API_KEY", "")
        if key:
            return key
    except Exception:
        pass
    raise RuntimeError("Missing API_KEY in config.py/.env")

API_KEY    = _load_api_key()
API_PORT   = 8080
LIVE_TRADING_ENABLED = os.environ.get("DDX_ENABLE_LIVE_TRADING", "0").strip().lower() in {"1", "true", "yes", "on"}
ADMIN_ID_SET = {int(uid) for uid in getattr(_cfg, "ADMIN_IDS", [])}

CORS_ALLOWED_ORIGINS: list[str] = [
    o.strip()
    for o in os.environ.get("CORS_ALLOWED_ORIGINS", "http://127.0.0.1:3000").split(",")
    if o.strip()
]

# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="DigitalDegenX AI Control API",
    description="Control the trading bot from any AI model or agent.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key"],
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

async def verify_key(key: str = Security(api_key_header)):
    if not secrets.compare_digest(key, API_KEY):
        raise HTTPException(status_code=403, detail="Invalid API key")
    return key


def _require_live_trading_enabled():
    if not LIVE_TRADING_ENABLED:
        raise HTTPException(status_code=503, detail="Live trading disabled during security lockdown")


def _require_admin_uid(uid: int):
    if int(uid) not in ADMIN_ID_SET:
        raise HTTPException(status_code=403, detail="Admin-only route")


async def _run_blocking(func, /, *args, **kwargs):
    """Run blocking bot/db helpers off the event loop."""
    return await asyncio.to_thread(partial(func, *args, **kwargs))


PORTFOLIO_FETCH_TIMEOUT_SECONDS = 2.5
WATCHLIST_PREVIEW_TIMEOUT_SECONDS = 0.75
PORTFOLIO_FETCH_CONCURRENCY = 8
WATCHLIST_ENRICH_CONCURRENCY = 12


async def _with_timeout(awaitable, timeout: float, default):
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except Exception:
        return default


def _load_live_wallet_meta() -> dict:
    try:
        with open(LIVE_WALLET_META_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_live_wallet_meta(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(LIVE_WALLET_META_FILE, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(LIVE_WALLET_META_FILE, 0o600)

# ─── Lazy imports from bot internals ─────────────────────────────────────────
# Deferred imports: bot.py/scanner.py take several seconds to initialize.
# Importing here (rather than at module level) lets the API server start
# accepting health checks before those modules are ready. Thread-safe via
# Python's import lock.

def _bot():
    import bot as b
    return b

def _sc():
    import scanner as s
    return s

# ─── Request / Response models ────────────────────────────────────────────────

class BuyRequest(BaseModel):
    uid: int                    # Telegram user ID (used to look up mode/portfolio)
    mint: str                   # Token mint address
    sol_amount: float           # SOL to spend
    mode: Optional[str] = None  # "paper" or "live" — overrides user's current mode

class SellRequest(BaseModel):
    uid: int
    mint: str
    pct: int = 100              # % of position to sell (1–100)
    mode: Optional[str] = None

class PortfolioResetRequest(BaseModel):
    uid: int

class AlertRequest(BaseModel):
    uid: int
    mint: str
    symbol: str
    direction: str              # "above" or "below"
    price_usd: float

class ThresholdRequest(BaseModel):
    uid: int
    watch_score: Optional[int] = None   # 1–100
    alert_score: Optional[int] = None   # 1–100

class AutoSellUpdate(BaseModel):
    enabled: Optional[bool] = None
    mult_targets: Optional[list[dict[str, Union[int, float, bool, str]]]] = None
    custom_targets: Optional[list[dict[str, Union[int, float, bool, str]]]] = None
    stop_loss: Optional[dict[str, Union[int, float, bool, str]]] = None
    trailing_stop: Optional[dict[str, Union[int, float, bool, str]]] = None
    trailing_tp: Optional[dict[str, Union[int, float, bool, str]]] = None
    time_exit: Optional[dict[str, Union[int, float, bool, str]]] = None
    breakeven_stop: Optional[dict[str, Union[int, float, bool, str]]] = None
    first_risk_off: Optional[dict[str, Union[int, float, bool, str]]] = None
    velocity_rollover: Optional[dict[str, Union[int, float, bool, str]]] = None
    mcap_alerts: Optional[list[dict[str, Union[int, float, bool, str]]]] = None

class MessageRequest(BaseModel):
    uid: int
    text: str
    parse_mode: str = "Markdown"


class WalletAlertReq(BaseModel):
    address: str
    label: str = ""


class WalletCreateRequest(BaseModel):
    uid: int
    word_count: int = 12
    derivation_path: str = "m/44'/501'/0'/0'"
    bip39_passphrase: str = ""


class WalletImportRequest(BaseModel):
    uid: int
    mnemonic: str = ""
    private_key: str = ""
    derivation_path: str = "m/44'/501'/0'/0'"
    bip39_passphrase: str = ""


class SniperConfigUpdate(BaseModel):
    # Core trade settings
    enabled: Optional[bool] = None
    sol_amount: Optional[float] = None
    max_concurrent: Optional[int] = None
    take_profit_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    max_age_secs: Optional[int] = None
    dev_buy_max_pct: Optional[float] = None
    # Intelligence filters
    require_narrative: Optional[bool] = None
    min_predictor_confidence: Optional[float] = None
    use_lifecycle_filter: Optional[bool] = None
    max_bundle_risk: Optional[int] = None
    # Adaptive sizing
    sol_multiplier_narrative: Optional[float] = None
    sol_multiplier_predictor: Optional[float] = None
    # Scheduling
    active_hours_utc: Optional[str] = None
    # Notifications
    telegram_notify: Optional[bool] = None


class AutoBuyUpdate(BaseModel):
    enabled: Optional[bool] = None
    sol_amount: Optional[float] = None
    max_sol_amount: Optional[float] = None
    min_confidence: Optional[float] = None
    confidence_scale_enabled: Optional[bool] = None
    min_score: Optional[int] = None
    max_mcap: Optional[float] = None
    min_mcap_usd: Optional[float] = None
    daily_limit_sol: Optional[float] = None
    max_positions: Optional[int] = None
    max_narrative_exposure: Optional[int] = None
    max_archetype_exposure: Optional[int] = None
    buy_tier: Optional[str] = None
    min_liquidity_usd: Optional[float] = None
    max_liquidity_usd: Optional[float] = None
    min_age_mins: Optional[int] = None
    max_age_mins: Optional[int] = None
    min_txns_5m: Optional[int] = None


class SettingsUpdate(BaseModel):
    settings: dict[str, Union[int, float, bool, str]]


class ModeUpdate(BaseModel):
    uid: int
    mode: str


class TradeControlsUpdate(BaseModel):
    presets_enabled: Optional[bool] = None
    presets: Optional[list[dict[str, Union[int, float, bool, str]]]] = None
    global_stop_loss: Optional[dict[str, Union[int, float, bool, str]]] = None
    global_trailing_stop: Optional[dict[str, Union[int, float, bool, str]]] = None
    global_trailing_tp: Optional[dict[str, Union[int, float, bool, str]]] = None
    global_breakeven_stop: Optional[dict[str, Union[int, float, bool, str]]] = None
    global_time_exit: Optional[dict[str, Union[int, float, bool, str]]] = None


class StrategyProfilesUpdate(BaseModel):
    # keys are profile names; values are section dicts (trailing_stop, first_risk_off, etc.)
    overrides: dict[str, dict[str, dict[str, Union[int, float, bool, str]]]]

# ─── Bot reference (set from bot.py after startup) ───────────────────────────
_app_ref = None   # telegram Application instance

def set_bot_app(application):
    """Called from bot.py post_init to give us a handle to the running bot."""
    global _app_ref
    _app_ref = application


def _derive_buy_price_usd(sol_amount: float, out_raw: int, decimals: int = 6) -> float:
    if sol_amount <= 0 or out_raw <= 0:
        return 0.0
    try:
        import pumpfun as _pf_mod
        sol_price_usd = _pf_mod.get_sol_price() or 150.0
    except Exception:
        sol_price_usd = 150.0
    ui_tokens = out_raw / (10 ** decimals)
    return ((sol_amount * sol_price_usd) / ui_tokens) if ui_tokens > 0 else 0.0


async def _fetch_pair_for_mint(loop, bot_mod, mint: str):
    try:
        return await loop.run_in_executor(None, bot_mod.fetch_sol_pair, mint)
    except Exception:
        return None


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except Exception:
        return default


def _lifecycle_brief(mint: str) -> dict:
    snapshot = lifecycle_store.get_token_snapshot(mint)
    if not snapshot:
        return {}
    trading_snapshot = build_trading_snapshot(snapshot, max_age_hours=None)
    if not trading_snapshot:
        return {}
    return {
        "state": trading_snapshot.get("lifecycle_state"),
        "source_primary": trading_snapshot.get("source_primary"),
        "strategy_profile": trading_snapshot.get("strategy_profile"),
        "confidence": _safe_float(trading_snapshot.get("snapshot_confidence")),
        "age_mins": _safe_float(trading_snapshot.get("age_mins")),
        "buy_ratio_5m": _safe_float(trading_snapshot.get("buy_ratio_5m")),
        "holder_concentration": _safe_float(trading_snapshot.get("holder_concentration")),
    }


async def _enrich_token_rows(rows: list[dict], uid: Optional[int] = None) -> list[dict]:
    autobuy_mod = None
    if uid:
        import autobuy as autobuy_mod

    semaphore = asyncio.Semaphore(WATCHLIST_ENRICH_CONCURRENCY)

    async def enrich_one(row: dict) -> dict:
        item = dict(row)
        mint = str(item.get("mint") or "").strip()
        snapshot = lifecycle_store.get_token_snapshot(mint) if mint else None
        if mint:
            item.update(_lifecycle_brief(mint))
        if mint:
            # Always prefer the most recent mcap from the lifecycle snapshot over
            # any stale value stored at scan time in scanner_log / watchlist.
            trading_snapshot = build_trading_snapshot(snapshot, max_age_hours=None) if snapshot else None
            live_mcap = _safe_float(trading_snapshot.get("mcap")) if trading_snapshot else None
            if live_mcap:
                item["mcap"] = live_mcap
            elif not _safe_float(item.get("mcap")):
                # No live snapshot available — fall back to latest DB row
                latest_scan_row = await _run_blocking(_db.get_latest_scan_log_for_mint, mint)
                if latest_scan_row and latest_scan_row.get("mcap"):
                    item["mcap"] = _safe_float(latest_scan_row.get("mcap"))
        if uid and mint and snapshot and autobuy_mod:
            try:
                preview = await _with_timeout(
                    autobuy_mod.evaluate_lifecycle_snapshot(uid, snapshot, skip_freshness=True),
                    WATCHLIST_PREVIEW_TIMEOUT_SECONDS,
                    None,
                )
                if preview is None:
                    raise TimeoutError("preview timeout")
                item["autobuy_preview"] = {
                    "eligible": bool(preview.gate_passed),
                    "status": "eligible" if preview.gate_passed else "blocked",
                    "block_reason": preview.block_reason,
                    "block_category": preview.block_category,
                    "sol_amount": float(preview.sol_amount or 0),
                    "confidence": float(preview.confidence or 0),
                    "strategy_profile": preview.strategy_profile or item.get("strategy_profile"),
                }
            except Exception as exc:
                item["autobuy_preview"] = {
                    "eligible": False,
                    "status": "error",
                    "block_reason": str(exc),
                    "block_category": "preview_error",
                    "sol_amount": 0.0,
                    "confidence": float(item.get("confidence") or 0),
                    "strategy_profile": item.get("strategy_profile"),
                }
        return item

    async def guarded_enrich(row: dict) -> dict:
        async with semaphore:
            return await enrich_one(row)

    if not rows:
        return []
    return await asyncio.gather(*(guarded_enrich(row) for row in rows))


def _normalize_mult_targets(targets: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for target in targets or []:
        try:
            mult = float(target.get("mult") or target.get("multiplier") or 0)
            sell_pct = int(float(target.get("sell_pct") or target.get("pct") or 0))
        except Exception:
            continue
        if mult <= 0 or sell_pct <= 0:
            continue
        normalized.append(
            {
                "mult": mult,
                "sell_pct": sell_pct,
                "triggered": bool(target.get("triggered", False)),
                "label": str(target.get("label") or f"{mult:g}x"),
            }
        )
    return normalized


def _normalize_presets(presets: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for preset in presets or []:
        try:
            mult = float(preset.get("mult") or preset.get("multiplier") or 0)
            sell_pct = int(float(preset.get("sell_pct") or preset.get("pct") or 0))
        except Exception:
            continue
        if mult <= 0 or sell_pct <= 0:
            continue
        normalized.append({"mult": mult, "sell_pct": sell_pct})
    return normalized


def merge_autosell_update(config: dict, update: AutoSellUpdate) -> dict:
    merged = deepcopy(config or {})
    payload = update.model_dump(exclude_none=True)
    if "enabled" in payload:
        merged["enabled"] = bool(payload["enabled"])

    dict_blocks = {
        "stop_loss",
        "trailing_stop",
        "trailing_tp",
        "time_exit",
        "breakeven_stop",
        "first_risk_off",
        "velocity_rollover",
    }
    list_blocks = {"mult_targets", "custom_targets", "mcap_alerts"}

    for key, value in payload.items():
        if key == "enabled":
            continue
        if key in dict_blocks and isinstance(value, dict):
            current = deepcopy(merged.get(key) or {})
            current.update(value)
            merged[key] = current
        elif key in list_blocks and isinstance(value, list):
            if key == "mult_targets":
                merged[key] = _normalize_mult_targets(value)
            else:
                merged[key] = deepcopy(value)

    return merged


async def _build_paper_portfolio_view(uid: int) -> dict:
    """Return an enriched paper portfolio view suitable for the dashboard."""
    b = _bot()
    portfolio = await _run_blocking(b.get_portfolio, uid) or {}
    sol_balance = _safe_float(portfolio.get("SOL", 0))
    pumpfun_mod = getattr(b, "pumpfun", None)
    try:
        sol_usd = await _run_blocking(
            lambda: _safe_float(pumpfun_mod.get_sol_price() if pumpfun_mod else None, 150.0) or 150.0
        )
    except Exception:
        sol_usd = 150.0

    raw_positions = [(mint, amount) for mint, amount in portfolio.items() if mint != "SOL" and _safe_float(amount) > 0]
    if not raw_positions:
        return {
            "sol_balance": sol_balance,
            "sol_price_usd": sol_usd,
            "positions": [],
            "total_value_sol": sol_balance,
            "total_value_usd": sol_balance * sol_usd,
        }

    semaphore = asyncio.Semaphore(PORTFOLIO_FETCH_CONCURRENCY)

    async def fetch_price_data(mint: str) -> dict:
        async with semaphore:
            return await _with_timeout(
                b._fetch_portfolio_token_data(mint),
                PORTFOLIO_FETCH_TIMEOUT_SECONDS,
                {},
            )

    price_data = await asyncio.gather(*(fetch_price_data(mint) for mint, _ in raw_positions), return_exceptions=True)
    auto_sell_entries = await asyncio.gather(
        *[_run_blocking(b.get_auto_sell, uid, mint) for mint, _ in raw_positions],
        return_exceptions=True,
    )
    auto_sell_configs = {
        mint: (cfg if isinstance(cfg, dict) else {})
        for (mint, _), cfg in zip(raw_positions, auto_sell_entries)
    }

    positions = []
    total_value_sol = sol_balance
    total_value_usd = sol_balance * sol_usd

    for (mint, raw_amount), data in zip(raw_positions, price_data):
        try:
            payload = data if isinstance(data, dict) else {}
            pair = payload.get("pair") or {}
            bc = payload.get("bc") or {}
            coin = payload.get("coin") or {}
            cfg = auto_sell_configs.get(mint, {})

            base = pair.get("baseToken") or {}
            decimals = int(base.get("decimals") or cfg.get("decimals") or 6)
            ui_amount = _safe_float(raw_amount) / (10 ** max(decimals, 0))

            price_sol = _safe_float(pair.get("priceNative"))
            price_usd = _safe_float(pair.get("priceUsd"))
            virtual_token_reserves = _safe_float(bc.get("virtual_token_reserves"))
            if not price_sol and virtual_token_reserves > 0:
                try:
                    price_sol = (
                        _safe_float(bc.get("virtual_sol_reserves"))
                        / virtual_token_reserves
                        / 1e9
                        * 1e6
                    )
                except Exception:
                    price_sol = 0.0
            if not price_usd and price_sol:
                price_usd = price_sol * sol_usd

            value_sol = price_sol * ui_amount if price_sol else 0.0
            value_usd = price_usd * ui_amount if price_usd else 0.0
            total_value_sol += value_sol
            total_value_usd += value_usd

            buy_price_usd = _safe_float(cfg.get("buy_price_usd"))
            entry_sol_amt = _safe_float(cfg.get("sol_amount"))
            pnl_pct = None
            # Prefer SOL-denominated P&L — this is what the user actually cares about
            # since the bot trades in SOL, not USD.  USD-based P&L diverges badly when
            # the SOL/USD rate moves significantly between entry and now.
            if entry_sol_amt > 0 and value_sol > 0:
                pnl_pct = ((value_sol - entry_sol_amt) / entry_sol_amt) * 100.0
            elif buy_price_usd > 0 and price_usd > 0:
                # Fallback to USD-based P&L when SOL entry amount isn't available
                pnl_pct = ((price_usd - buy_price_usd) / buy_price_usd) * 100.0

            next_target = None
            for target in cfg.get("mult_targets", []) or []:
                if not target.get("triggered"):
                    next_target = target.get("label") or f"{target.get('multiplier', 0)}x"
                    break

            symbol = (
                cfg.get("symbol")
                or base.get("symbol")
                or coin.get("symbol")
                or mint[:8]
            )
            name = (
                cfg.get("name")
                or base.get("name")
                or coin.get("name")
                or symbol
            )
            mcap = _safe_float(pair.get("marketCap") or coin.get("usd_market_cap") or coin.get("market_cap"))

            positions.append(
                {
                    "mint": mint,
                    "symbol": symbol,
                    "name": name,
                    "raw_amount": _safe_float(raw_amount),
                    "ui_amount": ui_amount,
                    "decimals": decimals,
                    "price_sol": price_sol or None,
                    "price_usd": price_usd or None,
                    "value_sol": value_sol or None,
                    "value_usd": value_usd or None,
                    "buy_price_usd": buy_price_usd or None,
                    "pnl_pct": pnl_pct,
                    "mcap": mcap or None,
                    "auto_sell_enabled": bool(cfg.get("enabled")),
                    "entry_sol": _safe_float(cfg.get("sol_amount")) or None,
                    "next_target": next_target,
                    "narrative": cfg.get("narrative") or None,
                    "strategy_profile": cfg.get("strategy_profile") or None,
                    "exit_profile": cfg.get("exit_profile") or None,
                    "purchase_timestamp": _safe_float(cfg.get("purchase_timestamp")) or None,
                    "target_count": len(cfg.get("mult_targets", []) or []),
                    "stop_loss_enabled": bool((cfg.get("stop_loss") or {}).get("enabled")),
                    "trailing_stop_enabled": bool((cfg.get("trailing_stop") or {}).get("enabled")),
                    "first_risk_off_enabled": bool((cfg.get("first_risk_off") or {}).get("enabled")),
                }
            )
        except Exception as exc:
            positions.append(
                {
                    "mint": mint,
                    "symbol": mint[:8],
                    "name": mint[:8],
                    "raw_amount": _safe_float(raw_amount),
                    "ui_amount": _safe_float(raw_amount) / 1e6,
                    "decimals": 6,
                    "price_sol": None,
                    "price_usd": None,
                    "value_sol": None,
                    "value_usd": None,
                    "buy_price_usd": None,
                    "pnl_pct": None,
                    "mcap": None,
                    "auto_sell_enabled": False,
                    "entry_sol": None,
                    "next_target": None,
                    "narrative": None,
                    "strategy_profile": None,
                    "exit_profile": None,
                    "purchase_timestamp": None,
                    "target_count": 0,
                    "stop_loss_enabled": False,
                    "trailing_stop_enabled": False,
                    "first_risk_off_enabled": False,
                    "error": str(exc),
                }
            )

    positions.sort(key=lambda item: _safe_float(item.get("value_sol")), reverse=True)

    return {
        "sol_balance": sol_balance,
        "sol_price_usd": sol_usd,
        "positions": positions,
        "total_value_sol": total_value_sol,
        "total_value_usd": total_value_usd,
    }

# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/status", dependencies=[Depends(verify_key)])
async def status():
    """Bot health check."""
    return {
        "status":    "online",
        "timestamp": time.time(),
        "bot":       "DigitalDegenX",
    }


@app.get("/portfolio", dependencies=[Depends(verify_key)])
async def get_portfolio(uid: int):
    """Return raw and enriched paper portfolio data for a user."""
    b = _bot()
    portfolio = await _run_blocking(b.get_portfolio, uid)
    try:
        paper_view = await _build_paper_portfolio_view(uid)
    except Exception as exc:
        paper_view = {
            "sol_balance": _safe_float(portfolio.get("SOL", 0)),
            "sol_price_usd": 150.0,
            "positions": [],
            "total_value_sol": _safe_float(portfolio.get("SOL", 0)),
            "total_value_usd": _safe_float(portfolio.get("SOL", 0)) * 150.0,
            "error": str(exc),
        }
    return {"uid": uid, "portfolio": portfolio, "paper": paper_view}


@app.post("/portfolio/reset", dependencies=[Depends(verify_key)])
async def reset_paper_portfolio(req: PortfolioResetRequest):
    """Reset the user's paper wallet and clear paper position tracking."""
    b = _bot()
    _require_admin_uid(req.uid)
    b.reset_portfolio(req.uid)
    portfolio = b.get_portfolio(req.uid)
    paper_view = await _build_paper_portfolio_view(req.uid)
    return {"uid": req.uid, "portfolio": portfolio, "paper": paper_view}


@app.get("/mode", dependencies=[Depends(verify_key)])
async def get_user_mode(uid: int):
    """Return the current trading mode for a user."""
    b = _bot()
    return {"uid": uid, "mode": await _run_blocking(b.get_mode, uid)}


@app.post("/mode", dependencies=[Depends(verify_key)])
async def set_user_mode(req: ModeUpdate):
    """Update the user's current trading mode."""
    b = _bot()
    _require_admin_uid(req.uid)
    chosen = str(req.mode or "").strip().lower()
    if chosen not in {"paper", "live"}:
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    if chosen == "live":
        _require_live_trading_enabled()
    if chosen == "live" and not getattr(b, "WALLET_PRIVATE_KEY", None):
        raise HTTPException(status_code=400, detail="Live mode requires a configured wallet")

    prev = b.get_mode(req.uid)
    b.user_modes[req.uid] = chosen
    b._save_user_modes()
    if chosen == "paper" and prev != "paper":
        b.reset_portfolio(req.uid)
    return {"uid": req.uid, "mode": chosen}


@app.get("/wallet", dependencies=[Depends(verify_key)])
async def get_wallet():
    """Return live wallet SOL balance and token accounts."""
    b      = _bot()
    pubkey = await _run_blocking(b.get_wallet_pubkey)
    if not pubkey:
        raise HTTPException(status_code=503, detail="No wallet configured")
    sol_bal = await _run_blocking(b.get_sol_balance, pubkey)
    tokens  = await _run_blocking(b.get_token_accounts, pubkey)
    return {"pubkey": pubkey, "sol": sol_bal, "tokens": tokens}


@app.get("/scanner/top", dependencies=[Depends(verify_key)])
async def scanner_top(limit: int = 10, uid: Optional[int] = None):
    """Return top scanner alerts from today, sorted by score and enriched for the active UID."""
    sc      = _sc()
    alerts  = await _run_blocking(sc.get_todays_alerts) or []
    alerts  = sorted(alerts, key=lambda x: -x.get("score", 0))[:limit]
    return {"count": len(alerts), "alerts": await _enrich_token_rows(alerts, uid=uid)}


@app.get("/scanner/watchlist", dependencies=[Depends(verify_key)])
async def scanner_watchlist(
    uid: Optional[int] = None,
    limit: int = Query(250, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Return all tokens currently on the watchlist, enriched for the active UID."""
    sc = _sc()
    wl = await _run_blocking(sc.get_watchlist) or {}
    items = sorted(wl.values(), key=lambda x: -x.get("score", 0))
    total_count = len(items)
    page = items[offset: offset + limit]
    return {
        "count": len(page),
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "tokens": await _enrich_token_rows(page, uid=uid),
    }


@app.get("/research-log", dependencies=[Depends(verify_key)])
async def get_research_log(limit: int = 100, uid: Optional[int] = None):
    """Return recent research log entries plus export metadata."""
    records = await _run_blocking(research_logger.load_research_log_json) or []
    if uid is not None:
        records = [record for record in records if int(record.get("user_id") or 0) == int(uid)]
    trimmed = list(reversed(records[-max(1, min(limit, 500)):]))
    csv_path = Path(research_logger.export_csv_path())
    return {
        "count": len(trimmed),
        "items": trimmed,
        "csv_filename": csv_path.name,
    }


@app.get("/history", dependencies=[Depends(verify_key)])
async def get_history(
    uid: int,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    mode: Optional[str] = None,
):
    """Return closed-trade history for a user."""
    rows = await _run_blocking(_db.get_closed_trades, uid, limit=limit, offset=offset, mode=mode)
    return {"uid": uid, "count": len(rows), "closed_trades": rows}


@app.get("/scanner/feed", dependencies=[Depends(verify_key)])
async def scanner_feed(limit: int = 50, uid: Optional[int] = None):
    """Return the rolling scanner feed, newest first."""
    import autobuy as autobuy_mod

    items: list[dict] = []
    seen_mints: set[str] = set()
    has_lifecycle = False
    lifecycle_items = lifecycle_store.list_recent_snapshots(limit=max(1, min(limit, 100)))
    for snapshot in lifecycle_items:
        try:
            token = build_trading_snapshot(snapshot, max_age_hours=4)
            if not token:
                continue
            mint = str(token.get("mint") or "").strip()
            if not mint or mint in seen_mints:
                continue
            latest_scan_row = _db.get_latest_scan_log_for_mint(mint)
            mcap = _safe_float(token.get("mcap")) or _safe_float((latest_scan_row or {}).get("mcap"))
            has_lifecycle = True
            row = {
                "id": None,
                "date": None,
                "ts": snapshot.lifecycle.last_trade_ts or snapshot.lifecycle.launch_ts or snapshot.lifecycle.last_updated_ts,
                "mint": mint,
                "name": token.get("name"),
                "symbol": token.get("symbol"),
                "score": int(_safe_float(token.get("snapshot_score_effective")) or _safe_float(token.get("snapshot_score_raw"))),
                "mcap": mcap,
                "narrative": token.get("lifecycle_narrative"),
                "archetype": token.get("lifecycle_archetype"),
                "alerted": 0,
                "dq": None,
                "state": token.get("lifecycle_state"),
                "source_primary": token.get("source_primary"),
                "strategy_profile": token.get("strategy_profile"),
                "confidence": _safe_float(token.get("snapshot_confidence")),
                "age_mins": _safe_float(token.get("age_mins")),
                "buy_ratio_5m": _safe_float(token.get("buy_ratio_5m")),
            }
            if uid:
                try:
                    preview = await autobuy_mod.evaluate_lifecycle_snapshot(uid, snapshot, skip_freshness=True)
                    row["autobuy_preview"] = {
                        "eligible": bool(preview.gate_passed),
                        "status": "eligible" if preview.gate_passed else "blocked",
                        "block_reason": preview.block_reason,
                        "block_category": preview.block_category,
                        "sol_amount": _safe_float(preview.sol_amount),
                        "confidence": _safe_float(preview.confidence),
                        "strategy_profile": preview.strategy_profile or row.get("strategy_profile"),
                    }
                except Exception as exc:
                    row["autobuy_preview"] = {
                        "eligible": False,
                        "status": "error",
                        "block_reason": str(exc),
                        "block_category": "preview_error",
                        "sol_amount": 0.0,
                        "confidence": _safe_float(row.get("confidence")),
                        "strategy_profile": row.get("strategy_profile"),
                    }
            items.append(row)
            seen_mints.add(mint)
            if len(items) >= limit:
                return {"count": len(items), "items": items, "source": "lifecycle+scanner_log"}
        except Exception:
            continue

    raw_items = _db.get_scan_log(limit=max(1, min(limit * 5, 500)))
    for item in raw_items:
        mint = str(item.get("mint") or "").strip()
        if not mint or mint in seen_mints:
            continue
        seen_mints.add(mint)
        items.append(item)
        if len(items) >= limit:
            break
    return {
        "count": len(items),
        "items": items,
        "source": "lifecycle+scanner_log" if has_lifecycle else "scanner_log",
    }


@app.get("/token/{mint}/snapshot", dependencies=[Depends(verify_key)])
async def token_snapshot(mint: str, uid: Optional[int] = None):
    """Return the normalized lifecycle snapshot for a token."""
    import autobuy as autobuy_mod

    snapshot = lifecycle_store.get_token_snapshot(mint)
    if not snapshot:
        # Token not in lifecycle DB — fetch live data and return a synthetic snapshot
        b = _bot()
        loop = asyncio.get_event_loop()
        pair = await loop.run_in_executor(None, b.fetch_sol_pair, mint)
        if not pair:
            raise HTTPException(status_code=404, detail="Token not found in lifecycle DB or on DexScreener")
        bt = pair.get("baseToken", {})
        symbol = bt.get("symbol", "")
        name = bt.get("name", "") or symbol
        mcap = float(pair.get("marketCap", 0) or 0)
        price_usd = float(pair.get("priceUsd", 0) or 0)
        volume = pair.get("volume") or {}
        liquidity = pair.get("liquidity") or {}
        txns = pair.get("txns") or {}
        age_mins = None
        if pair.get("pairCreatedAt"):
            import time as _time
            age_mins = round((_time.time() - pair["pairCreatedAt"] / 1000) / 60)
        return {
            "mint": mint,
            "source": "live_dexscreener",
            "lifecycle": {
                "mint": mint,
                "symbol": symbol,
                "name": name,
                "state": "unknown",
                "launch_ts": pair.get("pairCreatedAt", 0) and pair["pairCreatedAt"] / 1000,
                "last_trade_ts": None,
                "migration_ts": None,
                "raydium_pool": None,
                "dex_pair": pair.get("pairAddress"),
                "dev_wallet": None,
                "source_primary": pair.get("dexId", ""),
                "source_rank": None,
                "narrative": None,
                "archetype": None,
                "strategy_profile": None,
                "last_score": None,
                "last_effective_score": None,
                "last_confidence": None,
                "last_updated_ts": None,
            },
            "metrics": {
                "mint": mint,
                "price_usd": price_usd,
                "mcap": mcap,
                "volume_usd_h1": float(volume.get("h1", 0) or 0),
                "volume_usd_h6": float(volume.get("h6", 0) or 0),
                "volume_usd_h24": float(volume.get("h24", 0) or 0),
                "liquidity_usd": float(liquidity.get("usd", 0) or 0),
                "age_mins": age_mins,
                "txns_5m_buys": int((txns.get("m5") or {}).get("buys", 0) or 0),
                "txns_5m_sells": int((txns.get("m5") or {}).get("sells", 0) or 0),
                "price_change_h1": float((pair.get("priceChange") or {}).get("h1", 0) or 0),
                "price_change_h6": float((pair.get("priceChange") or {}).get("h6", 0) or 0),
                "price_change_h24": float((pair.get("priceChange") or {}).get("h24", 0) or 0),
            },
            "enrichment": {"mint": mint, "rugcheck": {}, "dex": {}, "pump": {}, "wallet": {}, "updated_ts": None},
            "events": [],
            "trading_snapshot": None,
            "analysis": None,
            "autobuy_preview": None,
        }
    payload = snapshot.as_dict()
    trading_snapshot = build_trading_snapshot(snapshot, max_age_hours=None)
    if trading_snapshot and not float(trading_snapshot.get("mcap") or 0):
        latest_scan_row = _db.get_latest_scan_log_for_mint(mint)
        if latest_scan_row and latest_scan_row.get("mcap"):
            trading_snapshot["mcap"] = float(latest_scan_row.get("mcap") or 0)
    payload["trading_snapshot"] = trading_snapshot
    payload["analysis"] = None
    payload["autobuy_preview"] = None

    if trading_snapshot:
        try:
            scanner = _sc()
            rc = snapshot.enrichment.rugcheck or {}
            result = scanner.calculate_heat_score_with_settings(trading_snapshot, rc)
            effective_score = result.get("effective_score", result.get("total", 0))
            narrative = (
                result.get("matched_narrative")
                or trading_snapshot.get("lifecycle_narrative")
                or "Other"
            )
            quality = scanner.build_entry_quality(trading_snapshot, rc, result, narrative)
            quality_flags = scanner.apply_entry_quality_rules(
                quality,
                effective_score=float(effective_score or 0),
                momentum_alive=True,
            )
            payload["analysis"] = {
                "effective_score": effective_score,
                "raw_score": result.get("raw_total", result.get("total", 0)),
                "risk": result.get("risk"),
                "matched_narrative": narrative,
                "strategy_profile": quality_flags.get("strategy_profile") or result.get("strategy_profile"),
                "strategy_exit_preset": quality_flags.get("strategy_exit_preset") or result.get("strategy_exit_preset"),
                "strategy_size_bias": quality_flags.get("strategy_size_bias") or result.get("strategy_size_bias"),
                "strategy_confidence": result.get("strategy_confidence", result.get("archetype_conf", 0)),
                "archetype": result.get("archetype"),
                "archetype_label": result.get("archetype_label"),
                "breakdown": result.get("breakdown", {}),
                "entry_quality": quality,
                "quality_flags": quality_flags,
            }
        except Exception as exc:
            payload["analysis"] = {"error": str(exc)}

    if uid:
        try:
            preview = await autobuy_mod.evaluate_lifecycle_snapshot(uid, snapshot, skip_freshness=True)
            payload["autobuy_preview"] = {
                "eligible": bool(preview.gate_passed),
                "status": "eligible" if preview.gate_passed else "blocked",
                "block_reason": preview.block_reason,
                "block_category": preview.block_category,
                "sol_amount": float(preview.sol_amount or 0),
                "confidence": float(preview.confidence or 0),
                "strategy_profile": preview.strategy_profile or "",
                "mode": preview.mode,
            }
        except Exception as exc:
            payload["autobuy_preview"] = {
                "eligible": False,
                "status": "error",
                "block_reason": str(exc),
                "block_category": "preview_error",
                "sol_amount": 0.0,
                "confidence": 0.0,
                "strategy_profile": "",
                "mode": "",
            }
    return payload


@app.get("/token/{mint}/timeline", dependencies=[Depends(verify_key)])
async def token_timeline(mint: str, limit: int = 100):
    """Return lifecycle events for a token, newest first."""
    timeline = lifecycle_store.get_token_timeline(mint, limit=max(1, min(limit, 500)))
    return {"mint": mint, "count": len(timeline), "events": timeline}


@app.get("/price/{mint}", dependencies=[Depends(verify_key)])
async def token_price(mint: str):
    """Fetch live price and market cap for a token via DexScreener."""
    b    = _bot()
    loop = asyncio.get_event_loop()
    pair = await loop.run_in_executor(None, b.fetch_sol_pair, mint)
    if not pair:
        raise HTTPException(status_code=404, detail="Token not found on DexScreener")
    return {
        "mint":       pair["baseToken"]["address"],
        "symbol":     pair["baseToken"]["symbol"],
        "price_usd":  float(pair.get("priceUsd", 0) or 0),
        "price_sol":  float(pair.get("priceNative", 0) or 0),
        "mcap":       float(pair.get("marketCap", 0) or 0),
        "volume_h1":  float((pair.get("volume") or {}).get("h1", 0) or 0),
        "dex":        pair.get("dexId", ""),
    }


@app.get("/token/{mint}/safety", dependencies=[Depends(verify_key)])
async def token_safety(mint: str):
    """Run RugCheck safety analysis on a token."""
    b      = _bot()
    safety = await b.check_token_safety(mint)
    return safety


@app.post("/buy", dependencies=[Depends(verify_key)])
async def execute_buy(req: BuyRequest):
    """
    Execute a buy.
    - mode="paper" → simulated buy against paper portfolio
    - mode="live"  → real on-chain swap via Jupiter/pump.fun
    - mode omitted → uses the user's current mode setting
    """
    b    = _bot()
    loop = asyncio.get_event_loop()
    _require_admin_uid(req.uid)
    mode = req.mode or b.get_mode(req.uid)

    lamports = int(req.sol_amount * 1_000_000_000)

    if mode == "paper":
        # Paper buy
        quote = await loop.run_in_executor(
            None, lambda: b.jupiter_quote(b.SOL_MINT, req.mint, lamports, 150)
        )
        if not quote or "outAmount" not in quote:
            raise HTTPException(status_code=502, detail="Jupiter quote failed")
        out_raw   = int(quote["outAmount"])
        portfolio = b.get_portfolio(req.uid)
        sol_bal   = portfolio.get("SOL", 0)
        if sol_bal < req.sol_amount:
            raise HTTPException(status_code=400, detail=f"Insufficient paper SOL ({sol_bal:.4f})")
        portfolio["SOL"]      = sol_bal - req.sol_amount
        portfolio[req.mint]   = portfolio.get(req.mint, 0) + out_raw
        b.update_portfolio(req.uid, portfolio)
        pair = await _fetch_pair_for_mint(loop, b, req.mint)
        symbol = (pair or {}).get("baseToken", {}).get("symbol") or quote.get("outputMint", req.mint)[:8]
        price_usd = float((pair or {}).get("priceUsd", 0) or 0) or _derive_buy_price_usd(req.sol_amount, out_raw)
        b.setup_auto_sell(req.uid, req.mint, symbol, price_usd, out_raw, 6, sol_amount=req.sol_amount)
        b.log_trade(
            req.uid,
            "paper",
            "buy",
            req.mint,
            symbol,
            sol_amount=req.sol_amount,
            token_amount=out_raw,
            price_usd=price_usd,
        )
        return {"mode": "paper", "sol_spent": req.sol_amount, "tokens_received": out_raw,
                "new_sol_balance": portfolio["SOL"]}

    else:
        _require_live_trading_enabled()
        # Safety check
        safety = await b.check_token_safety(req.mint)
        if not safety["safe"]:
            raise HTTPException(status_code=400, detail=f"Safety check failed: {safety['block_reason']}")

        slippage = b.get_user_slippage(req.uid)
        sig, quote, attempts, final_slip = await b._swap_with_retry(
            b.SOL_MINT, req.mint, lamports, req.uid, loop
        )
        if "ERROR" in str(sig) or "error" in str(sig).lower():
            raise HTTPException(status_code=502, detail=f"Swap failed: {sig}")
        out_raw = int((quote or {}).get("outAmount", 0))
        pair = await _fetch_pair_for_mint(loop, b, req.mint)
        symbol = (pair or {}).get("baseToken", {}).get("symbol") or req.mint[:8]
        price_usd = float((pair or {}).get("priceUsd", 0) or 0) or _derive_buy_price_usd(req.sol_amount, out_raw)
        async with b._portfolio_lock(req.uid):
            portfolio = b.get_portfolio(req.uid)
            portfolio["SOL"] = max(0, portfolio.get("SOL", 0) - req.sol_amount)
            portfolio[req.mint] = portfolio.get(req.mint, 0) + out_raw
            b.update_portfolio(req.uid, portfolio)
        b.setup_auto_sell(req.uid, req.mint, symbol, price_usd, out_raw, 6, sol_amount=req.sol_amount)
        b.log_trade(
            req.uid,
            "live",
            "buy",
            req.mint,
            symbol,
            sol_amount=req.sol_amount,
            token_amount=out_raw,
            price_usd=price_usd,
            tx_sig=sig,
        )
        return {
            "mode":             "live",
            "tx_sig":           sig,
            "sol_spent":        req.sol_amount,
            "tokens_received":  out_raw,
            "slippage_bps":     final_slip,
            "attempts":         attempts,
        }


@app.post("/sell", dependencies=[Depends(verify_key)])
async def execute_sell(req: SellRequest):
    """
    Execute a sell (pct% of current position).
    """
    b    = _bot()
    loop = asyncio.get_event_loop()
    _require_admin_uid(req.uid)
    mode = req.mode or b.get_mode(req.uid)
    pct  = max(1, min(100, req.pct))

    # Guard: reject if check_auto_sell already has a swap in-flight for this position.
    if not b._claim_sell(req.uid, req.mint):
        raise HTTPException(status_code=409, detail="A sell for this position is already in progress — try again in a moment")
    try:
        if mode == "paper":
            pair = await loop.run_in_executor(None, b.fetch_sol_pair, req.mint)
            if not pair:
                raise HTTPException(status_code=404, detail="Token not found")
            price_sol = float(pair.get("priceNative", 0) or 0)
            as_cfg    = b._db.get_auto_sell(req.uid, req.mint) or {}
            buy_price = as_cfg.get("buy_price_usd") or b._get_buy_price(req.uid, req.mint)
            exit_metrics = b.build_exit_trade_metrics(
                req.uid,
                req.mint,
                float(pair.get("priceUsd", 0) or 0),
                reason="manual",
                as_cfg=as_cfg,
            )
            # Acquire portfolio lock for the atomic read-modify-write
            async with b._portfolio_lock(req.uid):
                portfolio = b.get_portfolio(req.uid)
                raw_held  = portfolio.get(req.mint, 0)
                if raw_held <= 0:
                    raise HTTPException(status_code=400, detail="No position to sell")
                sell_raw  = max(1, int(raw_held * pct / 100))
                dec       = int(pair.get("baseToken", {}).get("decimals", 6) or 6)
                ui        = sell_raw / (10 ** dec)
                sol_recv  = price_sol * ui * 0.99
                portfolio[req.mint] = raw_held - sell_raw
                portfolio["SOL"]    = portfolio.get("SOL", 0) + sol_recv
                if portfolio[req.mint] <= 0:
                    portfolio.pop(req.mint, None)
                b.update_portfolio(req.uid, portfolio)
            b.log_trade(req.uid, "paper", "sell", req.mint, pair["baseToken"]["symbol"],
                        sol_received=sol_recv, token_amount=sell_raw,
                        price_usd=float(pair.get("priceUsd", 0) or 0),
                        buy_price_usd=buy_price, **exit_metrics)
            return {"mode": "paper", "pct": pct, "tokens_sold": sell_raw,
                    "sol_received": sol_recv, "new_sol_balance": portfolio["SOL"]}

        else:
            _require_live_trading_enabled()
            pubkey   = b.get_wallet_pubkey()
            accounts = b.get_token_accounts(pubkey) if pubkey else []
            accounts = accounts or []
            held     = next((a for a in accounts if a["mint"] == req.mint), None)
            if not held:
                raise HTTPException(status_code=400, detail="Token not in live wallet")
            raw_held  = held["amount"]
            sell_raw  = max(1, int(raw_held * pct / 100))
            sig, quote, attempts, final_slip = await b._swap_with_retry(
                req.mint, b.SOL_MINT, sell_raw, req.uid, loop
            )
            if "ERROR" in str(sig) or "error" in str(sig).lower():
                raise HTTPException(status_code=502, detail=f"Swap failed: {sig}")
            sol_recv = int((quote or {}).get("outAmount", 0)) / 1e9
            pair = await _fetch_pair_for_mint(loop, b, req.mint)
            symbol = (pair or {}).get("baseToken", {}).get("symbol") or req.mint[:8]
            price_usd = float((pair or {}).get("priceUsd", 0) or 0)
            as_cfg = b._db.get_auto_sell(req.uid, req.mint) or {}
            buy_price = as_cfg.get("buy_price_usd") or b._get_buy_price(req.uid, req.mint)
            exit_metrics = b.build_exit_trade_metrics(
                req.uid,
                req.mint,
                price_usd,
                reason="manual",
                as_cfg=as_cfg,
            )
            async with b._portfolio_lock(req.uid):
                portfolio = b.get_portfolio(req.uid)
                portfolio["SOL"] = portfolio.get("SOL", 0) + sol_recv
                current = portfolio.get(req.mint, 0)
                if sell_raw >= current:
                    portfolio.pop(req.mint, None)
                    b.remove_auto_sell(req.uid, req.mint)
                else:
                    portfolio[req.mint] = current - sell_raw
                b.update_portfolio(req.uid, portfolio)
        b.log_trade(
            req.uid,
            "live",
            "sell",
            req.mint,
            symbol,
            sol_received=sol_recv,
            token_amount=sell_raw,
            price_usd=price_usd,
            buy_price_usd=buy_price,
            tx_sig=sig,
            **exit_metrics,
        )
        return {
            "mode":         "live",
            "tx_sig":       sig,
            "pct":          pct,
            "tokens_sold":  sell_raw,
            "sol_received": sol_recv,
            "slippage_bps": final_slip,
            "attempts":     attempts,
        }
    finally:
        b._release_sell(req.uid, req.mint)


@app.post("/scanner/threshold", dependencies=[Depends(verify_key)])
async def set_threshold(req: ThresholdRequest):
    """Update watch and/or alert score thresholds for a user."""
    import scanner_manager as sm_mod
    _require_admin_uid(req.uid)
    sm = sm_mod.ScannerManager()
    user_settings = sm.get_user_settings(req.uid)
    changed = {}
    if req.watch_score is not None:
        user_settings["alert_scouted_threshold"] = req.watch_score
        changed["watch_score"] = req.watch_score
    if req.alert_score is not None:
        user_settings["alert_warm_threshold"]      = req.alert_score
        user_settings["alert_hot_threshold"]       = req.alert_score
        user_settings["alert_ultra_hot_threshold"] = req.alert_score
        changed["alert_score"] = req.alert_score
    sm.save_user_settings(req.uid, user_settings)
    return {"uid": req.uid, "updated": changed}


@app.get("/trades", dependencies=[Depends(verify_key)])
async def get_trades(
    uid: int,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    action: Optional[str] = None,
    mode: Optional[str] = None,
    filter_spec: Optional[str] = None,
):
    """Return recent trades for a user with optional filters."""
    if filter_spec:
        # Python-level filter requires fetching a larger set first
        trades = _db.get_trades(uid, limit=10000, offset=0, mode=mode, action=action)
        trades = tc.filter_trades(trades, filter_spec)
        trades = trades[offset: offset + limit]
    else:
        trades = _db.get_trades(uid, limit=limit, offset=offset, mode=mode, action=action)
    return {"uid": uid, "count": len(trades), "trades": trades}


@app.get("/trades/stats", dependencies=[Depends(verify_key)])
async def get_trade_stats(uid: int, filter_spec: Optional[str] = None):
    """Return summarized trade-center stats for a user."""
    trades = _db.get_trades(uid, limit=10000)
    filtered = tc.filter_trades(trades, filter_spec or "all")
    closed = tc.filter_closed_trades(_db.get_closed_trades(uid, limit=10000), filter_spec or "all")
    return {
        "uid": uid,
        "filter": filter_spec or "all",
        "summary": tc.summarize_trades(filtered, closed),
        "closed_count": len(closed),
        "cohorts": tc.summarize_closed_cohorts(closed),
    }


@app.get("/trades/weekly-report", dependencies=[Depends(verify_key)])
async def get_trade_weekly_report(uid: int, days: int = 7, filter_spec: Optional[str] = None):
    """Return a weekly optimization report built from closed trades."""
    closed = _db.get_closed_trades(uid, limit=10000)
    if filter_spec:
        closed = tc.filter_closed_trades(closed, filter_spec)
    report = tc.build_optimization_report(closed, window_days=max(1, min(days, 30)))
    return {
        "uid": uid,
        "filter": filter_spec or "all",
        "window_days": max(1, min(days, 30)),
        **report,
    }


@app.get("/intel/wallets", dependencies=[Depends(verify_key)])
async def get_wallet_intel(limit: int = 50):
    import intelligence_tracker as intel

    wallets = []
    for address, record in intel.get_auto_tracked_wallets().items():
        row = dict(record)
        row["address"] = address
        wallets.append(row)
    wallets.sort(key=lambda row: (-float(row.get("reputation", 0) or 0), -int(row.get("appearances", 0) or 0)))
    return {"count": len(wallets), "wallets": wallets[: max(1, min(limit, 200))]}


@app.get("/intel/narratives", dependencies=[Depends(verify_key)])
async def get_narrative_intel():
    import intelligence_tracker as intel

    items = []
    for name, record in intel.get_narrative_stats().items():
        row = dict(record)
        row["name"] = name
        items.append(row)
    items.sort(key=lambda row: (-float(row.get("trending_score", 0) or 0), -float(row.get("win_rate", 0) or 0)))
    return {"count": len(items), "narratives": items}


@app.get("/intel/discovery", dependencies=[Depends(verify_key)])
async def get_wallet_discovery(limit: int = 20):
    import wallet_discovery

    rows = wallet_discovery.get_top_discovered(limit=max(1, min(limit, 100))) or []
    return {"count": len(rows), "wallets": rows}


@app.get("/intel/clusters", dependencies=[Depends(verify_key)])
async def get_cluster_intel(limit: int = 20):
    import wallet_cluster

    top_pairs = wallet_cluster.get_global_top_clusters(limit=max(1, min(limit, 100))) or []
    cluster_log = wallet_cluster.get_cluster_log(limit=max(1, min(limit, 100))) or []
    return {
        "count": len(top_pairs),
        "top_pairs": top_pairs,
        "recent_events": cluster_log,
    }


@app.get("/intel/bundles", dependencies=[Depends(verify_key)])
async def get_bundle_intel(limit: int = 20):
    import wallet_fingerprint

    rows = wallet_fingerprint.get_bundle_log(limit=max(1, min(limit, 100))) or []
    return {"count": len(rows), "events": rows}


@app.get("/intel/playbook", dependencies=[Depends(verify_key)])
async def get_playbook_intel():
    import launch_predictor

    return launch_predictor.get_playbook_summary()


@app.get("/autobuy/{uid}", dependencies=[Depends(verify_key)])
async def get_autobuy(uid: int):
    """Return auto-buy configuration for a user."""
    b = _bot()
    return await _run_blocking(b.get_auto_buy, uid)


@app.get("/autobuy/activity/{uid}", dependencies=[Depends(verify_key)])
async def get_autobuy_activity(uid: int, limit: int = 20):
    """Return recent auto-buy decisions for dashboard observability."""
    rows = await _run_blocking(_db.get_auto_buy_activity, uid, limit=limit)
    latest = rows[0] if rows else None
    summary = await _run_blocking(_db.get_auto_buy_activity_summary, uid, window_hours=24)
    return {
        "uid": uid,
        "count": len(rows),
        "latest": latest,
        "summary": summary,
        "items": rows,
    }


@app.post("/autobuy/{uid}", dependencies=[Depends(verify_key)])
async def update_autobuy(uid: int, update: AutoBuyUpdate):
    """Update auto-buy configuration for a user."""
    b = _bot()
    _require_admin_uid(uid)
    cfg = b.get_auto_buy(uid)
    for field, value in update.model_dump(exclude_none=True).items():
        cfg[field] = value
    b.set_auto_buy(uid, cfg)
    return b.get_auto_buy(uid)


@app.get("/settings/{uid}", dependencies=[Depends(verify_key)])
async def get_settings(uid: int):
    """Return user scanner/heat-score settings."""
    return {"uid": uid, "settings": await _run_blocking(sm.get_user_settings, uid)}


@app.post("/settings/{uid}", dependencies=[Depends(verify_key)])
async def update_settings(uid: int, update: SettingsUpdate):
    """Persist partial heat-score settings for a user."""
    _require_admin_uid(uid)
    current = sm.get_user_settings(uid)
    current.update(update.settings or {})
    ok = sm.save_user_settings(uid, current)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to save settings")
    return {"uid": uid, "settings": sm.get_user_settings(uid)}


@app.get("/trade-controls/{uid}", dependencies=[Depends(verify_key)])
async def get_trade_controls(uid: int):
    """Return dashboard-facing trade controls: presets and global exit blocks."""
    b = _bot()
    return {
        "uid": uid,
        "presets_enabled": bool(await _run_blocking(b.get_user_as_presets_enabled, uid)),
        "presets": await _run_blocking(b.get_user_as_presets, uid),
        "global_stop_loss": await _run_blocking(b.get_global_sl),
        "global_trailing_stop": await _run_blocking(b.get_global_trailing_stop),
        "global_trailing_tp": await _run_blocking(b.get_global_trailing_tp),
        "global_breakeven_stop": await _run_blocking(b.get_global_breakeven_stop),
        "global_time_exit": await _run_blocking(b.get_global_time_exit),
    }


@app.post("/trade-controls/{uid}", dependencies=[Depends(verify_key)])
async def update_trade_controls(uid: int, update: TradeControlsUpdate):
    """Update dashboard-managed preset and global exit controls."""
    b = _bot()
    _require_admin_uid(uid)
    payload = update.model_dump(exclude_none=True)
    if "presets_enabled" in payload:
        b.set_user_as_presets_enabled(uid, bool(payload["presets_enabled"]))
    if "presets" in payload:
        normalized = _normalize_presets(payload["presets"])
        if not normalized:
            raise HTTPException(status_code=400, detail="At least one valid preset is required")
        b.set_user_as_presets(uid, normalized)
    if "global_stop_loss" in payload:
        b.set_global_sl(payload["global_stop_loss"])
    if "global_trailing_stop" in payload:
        b.set_global_trailing_stop(payload["global_trailing_stop"])
    if "global_trailing_tp" in payload:
        b.set_global_trailing_tp(payload["global_trailing_tp"])
    if "global_breakeven_stop" in payload:
        b.set_global_breakeven_stop(payload["global_breakeven_stop"])
    if "global_time_exit" in payload:
        b.set_global_time_exit(payload["global_time_exit"])
    return await get_trade_controls(uid)


@app.get("/strategy-profiles/{uid}", dependencies=[Depends(verify_key)])
async def get_strategy_profiles(uid: int):
    """Return strategy profile exit+entry defaults and the user's current overrides."""
    import strategy_profiles as sp
    b = _bot()
    user_exit_overrides = b.get_user_profile_overrides(uid)
    user_entry_overrides = b.get_user_profile_entry_overrides(uid)
    profiles = {}
    for name, exit_defaults in sp.PROFILE_EXIT_DEFAULTS.items():
        # Entry defaults from the canonical STRATEGY_PROFILES dict
        raw_profile = sp.STRATEGY_PROFILES.get(name, {})
        entry_defaults = {k: raw_profile[k] for k in sp.PROFILE_ENTRY_FIELDS if k in raw_profile}

        all_defaults = dict(exit_defaults)
        all_defaults["entry"] = entry_defaults

        # Merge effective = defaults + user overrides
        effective = {}
        for section, vals in exit_defaults.items():
            effective[section] = dict(vals)
        effective["entry"] = dict(entry_defaults)

        profile_exit_ov = user_exit_overrides.get(name, {})
        for section, vals in profile_exit_ov.items():
            effective.setdefault(section, {}).update(vals)

        profile_entry_ov = user_entry_overrides.get(name, {})
        if profile_entry_ov:
            effective["entry"].update(profile_entry_ov)

        combined_overrides = dict(profile_exit_ov)
        if profile_entry_ov:
            combined_overrides["entry"] = profile_entry_ov

        profiles[name] = {
            "defaults": all_defaults,
            "overrides": combined_overrides,
            "effective": effective,
        }
    return {"uid": uid, "profiles": profiles}


@app.post("/strategy-profiles/{uid}", dependencies=[Depends(verify_key)])
async def update_strategy_profiles(uid: int, update: StrategyProfilesUpdate):
    """Replace user overrides for one or more strategy profiles (exit + entry)."""
    import strategy_profiles as sp
    b = _bot()
    _require_admin_uid(uid)
    current_exit = b.get_user_profile_overrides(uid)
    current_entry = b.get_user_profile_entry_overrides(uid)
    for profile_name, sections in update.overrides.items():
        if profile_name not in sp.PROFILE_EXIT_DEFAULTS:
            raise HTTPException(status_code=400, detail=f"Unknown profile: {profile_name}")
        entry_section = sections.pop("entry", None)
        if sections:
            current_exit[profile_name] = {k: dict(v) for k, v in sections.items()}
        if entry_section is not None:
            current_entry[profile_name] = dict(entry_section)
    b.set_user_profile_overrides(uid, current_exit)
    b.set_user_profile_entry_overrides(uid, current_entry)
    return await get_strategy_profiles(uid)


@app.get("/autosell/{mint}", dependencies=[Depends(verify_key)])
async def get_autosell(mint: str, uid: int):
    """Return auto-sell config for a token."""
    b = _bot()
    cfg = b.get_auto_sell(uid, mint)
    if not cfg:
        raise HTTPException(status_code=404, detail="No auto-sell config for this token")
    return {
        "uid": uid,
        "mint": mint,
        "config": cfg,
    }


@app.post("/autosell/{mint}", dependencies=[Depends(verify_key)])
async def update_autosell(mint: str, uid: int, update: AutoSellUpdate):
    """Update auto-sell config for a token (partial update)."""
    b = _bot()
    _require_admin_uid(uid)
    cfg = b.get_auto_sell(uid, mint)
    if not cfg:
        raise HTTPException(status_code=404, detail="No auto-sell config for this token")
    merged = merge_autosell_update(cfg, update)
    try:
        import exit_logic

        exit_logic.ensure_exit_blocks(
            merged,
            narrative=merged.get("narrative"),
            entry_score_effective=merged.get("entry_score_effective"),
        )
    except Exception:
        pass
    b.set_auto_sell(uid, mint, merged)
    return {"mint": mint, "uid": uid, "updated": merged}


@app.post("/message", dependencies=[Depends(verify_key)])
async def send_message(req: MessageRequest):
    """Send a Telegram message to a user via the bot."""
    _require_admin_uid(req.uid)
    if _app_ref is None:
        raise HTTPException(status_code=503, detail="Bot not connected yet")
    try:
        await _app_ref.bot.send_message(req.uid, req.text, parse_mode=req.parse_mode)
        return {"sent": True, "uid": req.uid}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/score/{mint}", dependencies=[Depends(verify_key)])
async def score_token(mint: str):
    """Run a full heat score analysis on a token."""
    sc     = _sc()
    loop   = asyncio.get_event_loop()
    result = await sc.score_single_token(mint)
    if not result:
        raise HTTPException(status_code=404, detail="Token not found or could not be scored")
    return result


# ─── Sniper endpoints ─────────────────────────────────────────────────────────

def _sniper():
    import sniper as s
    return s


@app.get("/sniper/{uid}/status", dependencies=[Depends(verify_key)])
async def sniper_status(uid: int):
    """Quick status: enabled, open position count, today's P&L."""
    sn = _sniper()
    engine = sn.get_engine(uid)
    cfg = sn._get_config(uid)
    stats = engine.get_stats()
    b = _bot()
    return {
        "uid": uid,
        "enabled": cfg.enabled,
        "effective_mode": b.get_mode(uid),
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "live_wallet_configured": bool(getattr(b, "WALLET_PRIVATE_KEY", None)),
        **stats,
    }


@app.get("/sniper/{uid}/config", dependencies=[Depends(verify_key)])
async def sniper_get_config(uid: int):
    """Return sniper config for a user."""
    sn = _sniper()
    cfg = sn._get_config(uid)
    b = _bot()
    return {
        "uid": uid,
        "effective_mode": b.get_mode(uid),
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "live_wallet_configured": bool(getattr(b, "WALLET_PRIVATE_KEY", None)),
        # Core
        "enabled": cfg.enabled,
        "sol_amount": cfg.sol_amount,
        "max_concurrent": cfg.max_concurrent,
        "take_profit_pct": cfg.take_profit_pct,
        "stop_loss_pct": cfg.stop_loss_pct,
        "max_age_secs": cfg.max_age_secs,
        "dev_buy_max_pct": cfg.dev_buy_max_pct,
        # Intelligence filters
        "require_narrative": cfg.require_narrative,
        "min_predictor_confidence": cfg.min_predictor_confidence,
        "use_lifecycle_filter": cfg.use_lifecycle_filter,
        "max_bundle_risk": cfg.max_bundle_risk,
        # Adaptive sizing
        "sol_multiplier_narrative": cfg.sol_multiplier_narrative,
        "sol_multiplier_predictor": cfg.sol_multiplier_predictor,
        # Scheduling
        "active_hours_utc": cfg.active_hours_utc,
        # Notifications
        "telegram_notify": cfg.telegram_notify,
    }


@app.post("/sniper/{uid}/config", dependencies=[Depends(verify_key)])
async def sniper_update_config(uid: int, update: SniperConfigUpdate):
    """Update sniper config for a user (partial update)."""
    sn = _sniper()
    _require_admin_uid(uid)
    cfg = sn._get_config(uid)
    if update.enabled is not None:
        cfg.enabled = update.enabled
    if update.sol_amount is not None:
        cfg.sol_amount = max(0.001, update.sol_amount)
    if update.max_concurrent is not None:
        cfg.max_concurrent = max(1, min(20, update.max_concurrent))
    if update.take_profit_pct is not None:
        cfg.take_profit_pct = max(1.0, update.take_profit_pct)
    if update.stop_loss_pct is not None:
        cfg.stop_loss_pct = max(1.0, min(99.0, update.stop_loss_pct))
    if update.max_age_secs is not None:
        cfg.max_age_secs = max(30, update.max_age_secs)
    if update.dev_buy_max_pct is not None:
        cfg.dev_buy_max_pct = max(0.0, min(100.0, update.dev_buy_max_pct))
    if update.require_narrative is not None:
        cfg.require_narrative = update.require_narrative
    if update.min_predictor_confidence is not None:
        cfg.min_predictor_confidence = max(0.0, min(100.0, update.min_predictor_confidence))
    if update.use_lifecycle_filter is not None:
        cfg.use_lifecycle_filter = update.use_lifecycle_filter
    if update.max_bundle_risk is not None:
        cfg.max_bundle_risk = max(0, min(10, update.max_bundle_risk))
    if update.sol_multiplier_narrative is not None:
        cfg.sol_multiplier_narrative = max(0.1, min(10.0, update.sol_multiplier_narrative))
    if update.sol_multiplier_predictor is not None:
        cfg.sol_multiplier_predictor = max(0.1, min(10.0, update.sol_multiplier_predictor))
    if update.active_hours_utc is not None:
        cfg.active_hours_utc = update.active_hours_utc.strip()
    if update.telegram_notify is not None:
        cfg.telegram_notify = update.telegram_notify
    sn._save_config(cfg)
    return await sniper_get_config(uid)


@app.get("/sniper/{uid}/positions", dependencies=[Depends(verify_key)])
async def sniper_positions(uid: int):
    """Return open snipe positions with live unrealized P&L."""
    sn = _sniper()
    engine = sn.get_engine(uid)
    loop = asyncio.get_event_loop()
    positions = await loop.run_in_executor(None, engine.get_positions)
    return {"uid": uid, "count": len(positions), "positions": positions}


@app.get("/sniper/{uid}/history", dependencies=[Depends(verify_key)])
async def sniper_history(uid: int, limit: int = Query(50, ge=1, le=500)):
    """Return completed snipe trade history."""
    sn = _sniper()
    engine = sn.get_engine(uid)
    history = engine.get_history(limit=limit)
    return {"uid": uid, "count": len(history), "history": history}


@app.get("/sniper/{uid}/attempts", dependencies=[Depends(verify_key)])
async def sniper_attempts(uid: int, limit: int = Query(50, ge=1, le=500)):
    """Return recent sniper buy attempts, including failed live buys."""
    sn = _sniper()
    engine = sn.get_engine(uid)
    attempts = engine.get_buy_attempts(limit=limit)
    return {"uid": uid, "count": len(attempts), "attempts": attempts}


@app.post("/sniper/{uid}/close/{mint}", dependencies=[Depends(verify_key)])
async def sniper_close_position(uid: int, mint: str):
    """Manually close an open snipe position."""
    sn = _sniper()
    _require_admin_uid(uid)
    engine = sn.get_engine(uid)
    result = await engine.close_position(mint)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "not found"))
    return result


# ── Wallet settings ──────────────────────────────────────────────────────────

@app.get("/wallet/info", dependencies=[Depends(verify_key)])
async def wallet_info(uid: int = Query(...)):
    """Return live wallet public key, SOL balance, and backup status."""
    _require_admin_uid(uid)
    b = _bot()
    pubkey = b.get_wallet_pubkey()
    sol_bal = 0.0
    if pubkey:
        try:
            sol_bal = b.get_sol_balance(pubkey)
        except Exception:
            pass
    wallet_meta = _load_live_wallet_meta()
    backup_status = {"has_backup": False, "backup_type": None, "created_ts": None}
    try:
        from wallet_manager import get_wallet_backup_status
        backup_status = get_wallet_backup_status(pubkey)
    except Exception:
        pass
    return {
        "pubkey": pubkey or "",
        "sol": sol_bal,
        "has_backup": bool(backup_status.get("has_backup")),
        "backup_type": backup_status.get("backup_type"),
        "backup_created_ts": backup_status.get("created_ts"),
        "source": wallet_meta.get("source", "env" if pubkey else ""),
        "derivation_path": wallet_meta.get("derivation_path"),
        "mnemonic_word_count": wallet_meta.get("mnemonic_word_count"),
        "has_bip39_passphrase": bool(wallet_meta.get("has_bip39_passphrase")),
        "backup_mode": wallet_meta.get("backup_mode", "manual_offline" if wallet_meta.get("mnemonic_word_count") else None),
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "wallet_management_enabled": LIVE_TRADING_ENABLED,
        "supports_watch_only": True,
        "supports_multiple_live_wallets": False,
    }


@app.get("/wallet/capabilities", dependencies=[Depends(verify_key)])
async def wallet_capabilities(uid: int = Query(...)):
    """Return wallet feature support matrix for the current architecture."""
    _require_admin_uid(uid)
    return {
        "uid": uid,
        "live_trading_enabled": LIVE_TRADING_ENABLED,
        "capabilities": [
            {"key": "seed_phrase", "label": "BIP-39 seed phrase", "status": "supported", "detail": "12- and 24-word mnemonic generation/import."},
            {"key": "hd_derivation", "label": "HD derivation path", "status": "supported", "detail": "Custom BIP-44 style derivation path for Solana wallets."},
            {"key": "bip39_passphrase", "label": "BIP-39 passphrase", "status": "supported", "detail": "Optional passphrase changes the derived wallet and is never stored by the app."},
            {"key": "private_key_import", "label": "Private key import", "status": "supported", "detail": "Base58 Solana private key import for the live bot wallet."},
            {"key": "watch_only", "label": "Watch-only wallets", "status": "supported", "detail": "Tracked wallet alerts act as address-only watchlists."},
            {"key": "manual_backup", "label": "Manual offline seed backup", "status": "supported", "detail": "Mnemonic is shown once for offline storage and is not kept as a site-managed backup."},
            {"key": "multi_wallet", "label": "Multi-wallet live accounts", "status": "partial", "detail": "One live signing wallet plus many tracked watch-only wallets."},
            {"key": "hardware_wallet", "label": "Hardware wallet signing", "status": "unsupported", "detail": "Ledger/Trezor integration is not available in this server-side wallet model."},
            {"key": "secure_enclave", "label": "Secure enclave / biometric auth", "status": "unsupported", "detail": "Requires native device key custody, not this web dashboard."},
            {"key": "multisig", "label": "Multisig", "status": "unsupported", "detail": "Not implemented for the bot wallet flow."},
            {"key": "tx_simulation", "label": "Transaction simulation preview", "status": "unsupported", "detail": "The wallet settings page does not simulate contract effects before signing."},
        ],
    }


def _persist_live_wallet(req_uid: int, key_data: dict, source: str, backup_saved: bool = False) -> dict:
    b = _bot()
    b.save_wallet_key(key_data["private_key_base58"])
    meta = {
        "public_key": key_data["public_key"],
        "source": source,
        "derivation_path": key_data.get("path"),
        "mnemonic_word_count": key_data.get("word_count"),
        "has_bip39_passphrase": bool(key_data.get("has_bip39_passphrase")),
        "backup_mode": "manual_offline" if key_data.get("word_count") else None,
        "backup_saved": backup_saved,
        "updated_by_uid": int(req_uid),
        "updated_ts": int(time.time()),
    }
    _save_live_wallet_meta(meta)
    return meta


@app.post("/wallet/create", dependencies=[Depends(verify_key)])
async def wallet_create(req: WalletCreateRequest):
    """Create a live wallet from a new BIP-39 mnemonic and persist it as the bot wallet."""
    _require_admin_uid(req.uid)
    _require_live_trading_enabled()
    try:
        import wallet_manager as wm

        wallet_data = wm.create_wallet_with_mnemonic(
            word_count=req.word_count,
            backup_mode="manual",
            passphrase=req.bip39_passphrase,
            derivation_path=req.derivation_path,
        )
        meta = _persist_live_wallet(req.uid, wallet_data, source="mnemonic", backup_saved=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "uid": req.uid,
        "created": True,
        "wallet": {
            "public_key": wallet_data["public_key"],
            "derivation_path": wallet_data.get("path"),
            "mnemonic_word_count": wallet_data.get("word_count"),
            "has_bip39_passphrase": bool(req.bip39_passphrase),
            "backup_saved": False,
            "backup_type": None,
            "backup_mode": "manual_offline",
        },
        "secrets": {
            "mnemonic": wallet_data["mnemonic"],
            "private_key_base58": wallet_data["private_key_base58"],
            "recovery_code": wallet_data.get("recovery_code"),
        },
        "meta": meta,
        "security_notice": "Write down the mnemonic before leaving this screen. The dashboard does not keep a recoverable seed backup for site-created wallets.",
    }


@app.post("/wallet/import", dependencies=[Depends(verify_key)])
async def wallet_import(req: WalletImportRequest):
    """Import a live wallet from a seed phrase or base58 private key."""
    _require_admin_uid(req.uid)
    _require_live_trading_enabled()
    try:
        import wallet_manager as wm

        mnemonic = req.mnemonic.strip()
        private_key = req.private_key.strip()
        if bool(mnemonic) == bool(private_key):
            raise ValueError("Provide either mnemonic or private_key")

        if mnemonic:
            wallet_data = wm.recover_from_mnemonic(
                mnemonic,
                passphrase=req.bip39_passphrase,
                derivation_path=req.derivation_path,
            )
            wallet_data["word_count"] = len(mnemonic.split())
            wallet_data["has_bip39_passphrase"] = bool(req.bip39_passphrase)
            wallet_data["backup_saved"] = False
            meta = _persist_live_wallet(req.uid, wallet_data, source="mnemonic_import", backup_saved=False)
            return {
                "uid": req.uid,
                "imported": True,
                "wallet": {
                    "public_key": wallet_data["public_key"],
                    "derivation_path": wallet_data.get("path"),
                    "mnemonic_word_count": wallet_data.get("word_count"),
                    "has_bip39_passphrase": bool(req.bip39_passphrase),
                    "backup_saved": False,
                    "backup_type": None,
                    "backup_mode": "manual_offline",
                },
                "meta": meta,
                "security_notice": "Seed phrase imported and the derived private key is now the bot's live signing key. Keep the original seed phrase offline; the site will not retain it for recovery.",
            }

        wallet_data = wm.private_key_to_keypair(private_key)
        meta = _persist_live_wallet(req.uid, wallet_data, source="private_key_import", backup_saved=False)
        return {
            "uid": req.uid,
            "imported": True,
            "wallet": {
                "public_key": wallet_data["public_key"],
                "derivation_path": None,
                "mnemonic_word_count": None,
                "has_bip39_passphrase": False,
                "backup_saved": False,
                "backup_type": None,
                "backup_mode": None,
            },
            "meta": meta,
            "security_notice": "Raw private key imported as the bot's live signing key. No mnemonic backup is available from the app for this wallet.",
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/wallet/tracked/{uid}", dependencies=[Depends(verify_key)])
async def wallet_tracked_list(uid: int):
    """Return tracked wallet addresses for a user."""
    b = _bot()
    wallets = b.get_user_alert_wallets(uid)
    return {"uid": uid, "wallets": wallets}


@app.post("/wallet/tracked/{uid}", dependencies=[Depends(verify_key)])
async def wallet_tracked_add(uid: int, req: WalletAlertReq):
    """Add a wallet address to tracking."""
    b = _bot()
    _require_admin_uid(uid)
    address = req.address.strip()
    if not address:
        raise HTTPException(status_code=400, detail="address required")
    ok = b.add_user_alert_wallet(uid, address, req.label)
    if not ok:
        raise HTTPException(status_code=409, detail="already tracked or invalid address")
    return {"uid": uid, "added": address, "label": req.label or address[:8]}


@app.delete("/wallet/tracked/{uid}/{address}", dependencies=[Depends(verify_key)])
async def wallet_tracked_remove(uid: int, address: str):
    """Remove a wallet address from tracking."""
    b = _bot()
    _require_admin_uid(uid)
    b.remove_user_alert_wallet(uid, address)
    return {"uid": uid, "removed": address}
