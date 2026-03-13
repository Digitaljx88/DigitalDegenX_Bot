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

import os
import time
import asyncio
import secrets
from pathlib import Path
from typing import Optional, Union

from fastapi import FastAPI, HTTPException, Security, Depends
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import db as _db
import settings_manager as sm
import trade_center as tc
import research_logger

# ─── Config ───────────────────────────────────────────────────────────────────

DATA_DIR   = os.path.join(os.path.dirname(__file__), "data")
CONFIG_DIR = os.path.dirname(__file__)

def _load_api_key() -> str:
    try:
        import config as _cfg
        key = getattr(_cfg, "API_KEY", "")
        if key:
            return key
    except Exception:
        pass
    # Fallback: auto-generate
    key = secrets.token_hex(32)
    print(f"[API] No API_KEY in config.py — using temporary key: {key}", flush=True)
    return key

API_KEY    = _load_api_key()
API_PORT   = 8080

# ─── App ──────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="DigitalDegenX AI Control API",
    description="Control the trading bot from any AI model or agent.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

async def verify_key(key: str = Security(api_key_header)):
    if key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")
    return key

# ─── Lazy imports from bot internals ─────────────────────────────────────────
# We import lazily so the API can start even before bot.py fully loads.

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
    enabled: Optional[bool]    = None
    stop_loss_pct: Optional[int] = None
    targets: Optional[list]    = None   # list of {multiplier, pct}

class MessageRequest(BaseModel):
    uid: int
    text: str
    parse_mode: str = "Markdown"


class AutoBuyUpdate(BaseModel):
    enabled: Optional[bool] = None
    sol_amount: Optional[float] = None
    max_sol_amount: Optional[float] = None
    min_confidence: Optional[float] = None
    confidence_scale_enabled: Optional[bool] = None
    min_score: Optional[int] = None
    max_mcap: Optional[float] = None
    daily_limit_sol: Optional[float] = None
    max_positions: Optional[int] = None
    max_narrative_exposure: Optional[int] = None
    max_archetype_exposure: Optional[int] = None
    buy_tier: Optional[str] = None


class SettingsUpdate(BaseModel):
    settings: dict[str, Union[int, float, bool, str]]


class ModeUpdate(BaseModel):
    uid: int
    mode: str

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


async def _build_paper_portfolio_view(uid: int) -> dict:
    """Return an enriched paper portfolio view suitable for the dashboard."""
    b = _bot()
    portfolio = b.get_portfolio(uid) or {}
    sol_balance = _safe_float(portfolio.get("SOL", 0))
    pumpfun_mod = getattr(b, "pumpfun", None)
    try:
        sol_usd = _safe_float(pumpfun_mod.get_sol_price() if pumpfun_mod else None, 150.0) or 150.0
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

    price_data = await asyncio.gather(
        *[b._fetch_portfolio_token_data(mint) for mint, _ in raw_positions],
        return_exceptions=True,
    )
    auto_sell_configs = {mint: (b.get_auto_sell(uid, mint) or {}) for mint, _ in raw_positions}

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
            pnl_pct = None
            if buy_price_usd > 0 and price_usd > 0:
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
    portfolio = b.get_portfolio(uid)
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


@app.get("/mode", dependencies=[Depends(verify_key)])
async def get_user_mode(uid: int):
    """Return the current trading mode for a user."""
    b = _bot()
    return {"uid": uid, "mode": b.get_mode(uid)}


@app.post("/mode", dependencies=[Depends(verify_key)])
async def set_user_mode(req: ModeUpdate):
    """Update the user's current trading mode."""
    b = _bot()
    chosen = str(req.mode or "").strip().lower()
    if chosen not in {"paper", "live"}:
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
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
    pubkey = b.get_wallet_pubkey()
    if not pubkey:
        raise HTTPException(status_code=503, detail="No wallet configured")
    sol_bal = b.get_sol_balance(pubkey)
    tokens  = b.get_token_accounts(pubkey)
    return {"pubkey": pubkey, "sol": sol_bal, "tokens": tokens}


@app.get("/scanner/top", dependencies=[Depends(verify_key)])
async def scanner_top(limit: int = 10):
    """Return top scanner alerts from today, sorted by score."""
    sc      = _sc()
    alerts  = sc.get_todays_alerts() or []
    alerts  = sorted(alerts, key=lambda x: -x.get("score", 0))[:limit]
    return {"count": len(alerts), "alerts": alerts}


@app.get("/scanner/watchlist", dependencies=[Depends(verify_key)])
async def scanner_watchlist():
    """Return all tokens currently on the watchlist."""
    sc = _sc()
    wl = sc.get_watchlist() or {}
    items = sorted(wl.values(), key=lambda x: -x.get("score", 0))
    return {"count": len(items), "tokens": items}


@app.get("/research-log", dependencies=[Depends(verify_key)])
async def get_research_log(limit: int = 100, uid: Optional[int] = None):
    """Return recent research log entries plus export metadata."""
    records = research_logger.load_research_log_json() or []
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
async def get_history(uid: int, limit: int = 50, mode: Optional[str] = None):
    """Return closed-trade history for a user."""
    rows = _db.get_closed_trades(uid, limit=max(1, min(limit, 500)), mode=mode)
    return {"uid": uid, "count": len(rows), "closed_trades": rows}


@app.get("/scanner/feed", dependencies=[Depends(verify_key)])
async def scanner_feed(limit: int = 50):
    """Return the rolling scanner feed, newest first."""
    raw_items = _db.get_scan_log(limit=max(1, min(limit * 5, 500)))
    seen_mints: set[str] = set()
    items: list[dict] = []
    for item in raw_items:
        mint = str(item.get("mint") or "").strip()
        if not mint or mint in seen_mints:
            continue
        seen_mints.add(mint)
        items.append(item)
        if len(items) >= limit:
            break
    return {"count": len(items), "items": items}


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
    mode = req.mode or b.get_mode(req.uid)
    pct  = max(1, min(100, req.pct))

    if mode == "paper":
        portfolio = b.get_portfolio(req.uid)
        raw_held  = portfolio.get(req.mint, 0)
        if raw_held <= 0:
            raise HTTPException(status_code=400, detail="No position to sell")
        pair = await loop.run_in_executor(None, b.fetch_sol_pair, req.mint)
        if not pair:
            raise HTTPException(status_code=404, detail="Token not found")
        price_sol = float(pair.get("priceNative", 0) or 0)
        sell_raw  = max(1, int(raw_held * pct / 100))
        dec       = int(pair.get("baseToken", {}).get("decimals", 6) or 6)
        ui        = sell_raw / (10 ** dec)
        sol_recv  = price_sol * ui * 0.99
        as_cfg    = b._db.get_auto_sell(req.uid, req.mint) or {}
        buy_price = as_cfg.get("buy_price_usd") or b._get_buy_price(req.uid, req.mint)
        portfolio[req.mint] = raw_held - sell_raw
        portfolio["SOL"]    = portfolio.get("SOL", 0) + sol_recv
        if portfolio[req.mint] <= 0:
            portfolio.pop(req.mint, None)
        b.update_portfolio(req.uid, portfolio)
        exit_metrics = b.build_exit_trade_metrics(
            req.uid,
            req.mint,
            float(pair.get("priceUsd", 0) or 0),
            reason="manual",
            as_cfg=as_cfg,
        )
        b.log_trade(req.uid, "paper", "sell", req.mint, pair["baseToken"]["symbol"],
                    sol_received=sol_recv, token_amount=sell_raw,
                    price_usd=float(pair.get("priceUsd", 0) or 0),
                    buy_price_usd=buy_price, **exit_metrics)
        return {"mode": "paper", "pct": pct, "tokens_sold": sell_raw,
                "sol_received": sol_recv, "new_sol_balance": portfolio["SOL"]}

    else:
        pubkey   = b.get_wallet_pubkey()
        accounts = b.get_token_accounts(pubkey) if pubkey else []
        accounts = accounts or []
        held     = next((a for a in accounts if a["mint"] == req.mint), None)
        if not held:
            raise HTTPException(status_code=400, detail="Token not in live wallet")
        raw_held  = held["amount"]
        sell_raw  = max(1, int(raw_held * pct / 100))
        slippage  = b.get_user_slippage(req.uid)
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


@app.post("/scanner/threshold", dependencies=[Depends(verify_key)])
async def set_threshold(req: ThresholdRequest):
    """Update watch and/or alert score thresholds for a user."""
    import scanner_manager as sm_mod
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
    limit: int = 20,
    action: Optional[str] = None,
    mode: Optional[str] = None,
    filter_spec: Optional[str] = None,
):
    """Return recent trades for a user with optional filters."""
    trades = _db.get_trades(uid, limit=10000, mode=mode, action=action)
    if filter_spec:
        trades = tc.filter_trades(trades, filter_spec)
    trades = sorted(trades, key=lambda t: t.get("ts", 0), reverse=True)[: max(1, min(limit, 500))]
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
    return b.get_auto_buy(uid)


@app.post("/autobuy/{uid}", dependencies=[Depends(verify_key)])
async def update_autobuy(uid: int, update: AutoBuyUpdate):
    """Update auto-buy configuration for a user."""
    b = _bot()
    cfg = b.get_auto_buy(uid)
    for field, value in update.model_dump(exclude_none=True).items():
        cfg[field] = value
    b.set_auto_buy(uid, cfg)
    return {"uid": uid, "autobuy": b.get_auto_buy(uid)}


@app.get("/settings/{uid}", dependencies=[Depends(verify_key)])
async def get_settings(uid: int):
    """Return user scanner/heat-score settings."""
    return {"uid": uid, "settings": sm.get_user_settings(uid)}


@app.post("/settings/{uid}", dependencies=[Depends(verify_key)])
async def update_settings(uid: int, update: SettingsUpdate):
    """Persist partial heat-score settings for a user."""
    current = sm.get_user_settings(uid)
    current.update(update.settings or {})
    ok = sm.save_user_settings(uid, current)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to save settings")
    return {"uid": uid, "settings": sm.get_user_settings(uid)}


@app.get("/autosell/{mint}", dependencies=[Depends(verify_key)])
async def get_autosell(mint: str, uid: int):
    """Return auto-sell config for a token."""
    b   = _bot()
    cfg = b.load_auto_sell().get(str(uid), {}).get(mint)
    if not cfg:
        raise HTTPException(status_code=404, detail="No auto-sell config for this token")
    return cfg


@app.post("/autosell/{mint}", dependencies=[Depends(verify_key)])
async def update_autosell(mint: str, uid: int, update: AutoSellUpdate):
    """Update auto-sell config for a token (partial update)."""
    b    = _bot()
    data = b.load_auto_sell()
    cfg  = data.get(str(uid), {}).get(mint)
    if not cfg:
        raise HTTPException(status_code=404, detail="No auto-sell config for this token")
    if update.enabled is not None:
        cfg["enabled"] = update.enabled
    if update.stop_loss_pct is not None:
        cfg["stop_loss_pct"] = update.stop_loss_pct
    if update.targets is not None:
        cfg["targets"] = update.targets
    data.setdefault(str(uid), {})[mint] = cfg
    b.save_auto_sell(data)
    return {"mint": mint, "uid": uid, "updated": cfg}


@app.post("/message", dependencies=[Depends(verify_key)])
async def send_message(req: MessageRequest):
    """Send a Telegram message to a user via the bot."""
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
